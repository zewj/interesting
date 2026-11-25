"""Command-line interface: ``dcheck [FILES...]`` / ``dcheck --gen 3``."""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

from . import generate, runner
from .models import Result, Status

DEFAULT_LIST_FILE = "usernames.txt"
DEFAULT_OUT = "results"

_COLORS = {
    Status.AVAILABLE: "\033[32m",  # green
    Status.TAKEN: "\033[31m",      # red
    Status.INVALID: "\033[33m",    # yellow
    Status.ERROR: "\033[35m",      # magenta
}
_RESET = "\033[0m"

_OUT_FILES = {
    Status.AVAILABLE: "available.txt",
    Status.TAKEN: "taken.txt",
    Status.INVALID: "invalid.txt",
    Status.ERROR: "errors.txt",
}
# results in these files are final, so resuming skips them (errors get retried)
_DONE_STATUSES = (Status.AVAILABLE, Status.TAKEN, Status.INVALID)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dcheck",
        description="Bulk-check Discord username availability. Free names are saved to "
                    f"{DEFAULT_OUT}/available.txt as they are found.",
        epilog="examples:\n"
               "  dcheck usernames.txt            check a list (one name per line)\n"
               "  dcheck --gen 3                  find free 3-character names (a-z0-9)\n"
               "  dcheck --gen 4 --charset all    4c including _ and .\n"
               "  dcheck                          interactive menu (what the .exe shows)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="*", metavar="FILE",
                   help="files with usernames (newline/comma/space separated, # comments); "
                        f"'-' reads stdin; with nothing given, {DEFAULT_LIST_FILE} is used")
    g = p.add_argument_group("short-name generator")
    g.add_argument("--gen", type=int, nargs="+", choices=(2, 3, 4), metavar="N",
                   help="check every N-character name (2, 3 and/or 4); output goes to "
                        f"{DEFAULT_OUT}/<N>c-<charset>/ and resumes automatically")
    g.add_argument("--charset", choices=sorted(generate.CHARSETS), default="alnum",
                   help="characters for --gen: letters, digits, alnum (a-z0-9, default) "
                        "or all (a-z0-9_.)")
    g.add_argument("--shuffle", action="store_true",
                   help="check generated names in random order (always on for 4c)")
    g.add_argument("--fresh", action="store_true",
                   help="with --gen: ignore earlier progress and start over")
    g.add_argument("--limit", type=int, metavar="N", help="stop after checking N names")
    p.add_argument("-w", "--workers", type=int, default=3,
                   help="number of concurrent worker threads (default: 3)")
    p.add_argument("--delay", type=float, default=0.5,
                   help="minimum seconds between requests across all workers (default: 0.5)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="HTTP request timeout in seconds (default: 10)")
    p.add_argument("-o", "--out", metavar="DIR",
                   help="output folder for available.txt/taken.txt/invalid.txt/errors.txt "
                        f"(default: {DEFAULT_OUT})")
    p.add_argument("--resume", action="store_true",
                   help="skip names already in the output folder's result files")
    p.add_argument("--no-save", action="store_true", help="don't write the .txt result files")
    p.add_argument("--json", metavar="FILE", help="also write all results as JSON to FILE")
    p.add_argument("--csv", metavar="FILE", help="also write all results as CSV to FILE")
    p.add_argument("--only-available", action="store_true",
                   help="only print available usernames to the console")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--no-interactive", action="store_true",
                   help="never show the menu; exit with an error when no input is given")
    return p


def make_client(args: argparse.Namespace):
    """Create the username-checking client. Patched in tests."""
    from .client import DiscordClient

    return DiscordClient(timeout=args.timeout, min_interval=args.delay)


# -- input --------------------------------------------------------------------

def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(sys.stdin is not None and isatty and isatty())


def _read_files(files: Sequence[str]) -> str:
    chunks = []
    stdin_used = False
    for f in files:
        if f == "-":
            if not stdin_used:
                chunks.append(sys.stdin.read())
                stdin_used = True
        else:
            chunks.append(Path(f).read_text(encoding="utf-8-sig"))
    return "\n".join(chunks)


def _app_dir() -> Path:
    """Folder of the .exe when frozen (PyInstaller), else the current directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def find_default_list() -> Path | None:
    for folder in (Path.cwd(), _app_dir()):
        candidate = folder / DEFAULT_LIST_FILE
        if candidate.is_file():
            return candidate
    return None


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _read_pasted_list() -> str | None:
    print("Drag a .txt file here (or type its path), or paste usernames.")
    print("Finish with an empty line.\n")
    lines: list[str] = []
    while True:
        try:
            line = input("> " if not lines else "  ")
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    if not lines:
        return None
    if len(lines) == 1:
        candidate = lines[0].strip().strip('"').strip("'")
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text(encoding="utf-8-sig")
    return "\n".join(lines)


def interactive_menu(args: argparse.Namespace) -> str | None:
    """Menu shown when run with no input (e.g. the .exe was double-clicked).

    Returns list text to check, or sets ``args.gen``/``args.charset`` and returns "".
    Returns None if the user chose nothing.
    """
    default_list = find_default_list()
    print("dcheck - Discord username checker\n")
    if default_list:
        print(f"  [1] Check {default_list.name} ({default_list})")
    else:
        print(f"  [1] Check a list (paste names / drag a .txt here, "
              f"or put them in {DEFAULT_LIST_FILE} next to this program)")
    print("  [2] Find free 2-character names")
    print("  [3] Find free 3-character names")
    print("  [4] Find free 4-character names")
    choice = _ask("\nChoose 1-4: ").strip()
    if choice == "1":
        if default_list:
            return default_list.read_text(encoding="utf-8-sig")
        print()
        return _read_pasted_list()
    if choice in ("2", "3", "4"):
        args.gen = [int(choice)]
        extra = _ask("Include _ and . as well as a-z0-9? [y/N]: ").strip().lower()
        args.charset = "all" if extra.startswith("y") else "alnum"
        return ""
    return None


# -- output -------------------------------------------------------------------

class ResultWriter:
    """Appends each result to its status file as soon as it arrives (and flushes),
    so nothing is lost if the run is interrupted. Files are only created when needed."""

    def __init__(self, out: Path, *, resume: bool):
        self.out = out
        self._handles: dict[Status, object] = {}
        stale = _OUT_FILES.values() if not resume else [_OUT_FILES[Status.ERROR]]
        for fname in stale:  # errors are always retried, so start that file over
            (out / fname).unlink(missing_ok=True)

    def write(self, r: Result) -> None:
        fh = self._handles.get(r.status)
        if fh is None:
            self.out.mkdir(parents=True, exist_ok=True)
            fh = open(self.out / _OUT_FILES[r.status], "a", encoding="utf-8")
            self._handles[r.status] = fh
        fh.write(r.username + "\n")
        fh.flush()

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()


def load_done(out: Path) -> set[str]:
    done: set[str] = set()
    for status in _DONE_STATUSES:
        f = out / _OUT_FILES[status]
        if f.is_file():
            done.update(line.strip() for line in f.read_text(encoding="utf-8").splitlines()
                        if line.strip())
    return done


def _write_exports(args: argparse.Namespace, results: list[Result]) -> None:
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in results], fh, indent=2)
            fh.write("\n")
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["username", "status", "detail"])
            for r in results:
                w.writerow([r.username, r.status.value, r.detail])


def _use_color(args: argparse.Namespace) -> bool:
    if args.no_color or os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


def _format_line(r: Result, n: int, total: int, color: bool) -> str:
    tag = f"[{r.status.value.upper()}]"
    if color:
        tag = f"{_COLORS[r.status]}{tag}{_RESET}"
    width = len(str(total))
    line = f"({n:>{width}}/{total}) {tag} {r.username}"
    if r.detail and r.status is not Status.AVAILABLE:
        line += f" - {r.detail}"
    return line


def _fmt_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _finish(code: int, interactive: bool) -> int:
    """In interactive mode keep the console window open until the user presses Enter."""
    if interactive:
        try:
            input("\nPress Enter to exit...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def _enable_windows_ansi() -> None:
    """Turn on ANSI escape handling in the classic Windows console (no-op elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
        except Exception:
            pass


# -- main ---------------------------------------------------------------------

def _get_input(args: argparse.Namespace) -> tuple[str | None, bool]:
    """Return (list text or None, interactive?). May set args.gen via the menu."""
    if args.files:
        return _read_files(args.files), False
    if args.gen:
        return None, False
    if sys.stdin is not None and not _stdin_is_tty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped, False
    elif not args.no_interactive and _stdin_is_tty():
        return interactive_menu(args), True
    default_list = find_default_list()
    return (default_list.read_text(encoding="utf-8-sig") if default_list else None), False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    def usage_error(msg: str) -> int:
        parser.print_usage(sys.stderr)
        print(f"dcheck: error: {msg}", file=sys.stderr)
        return 2

    if args.workers < 1:
        return usage_error("--workers must be at least 1")
    if args.delay < 0 or args.timeout <= 0:
        return usage_error("--delay must be >= 0 and --timeout > 0")
    if args.limit is not None and args.limit < 1:
        return usage_error("--limit must be at least 1")

    _enable_windows_ansi()

    # 1. input
    interactive = False
    try:
        text, interactive = _get_input(args)
    except OSError as e:
        print(f"dcheck: error: cannot read input: {e}", file=sys.stderr)
        return 2
    gen_mode = bool(args.gen)
    if text is None and not gen_mode:
        print("dcheck: error: no input given (pass FILEs, '-', --gen N, pipe usernames on "
              f"stdin, or create {DEFAULT_LIST_FILE})", file=sys.stderr)
        return _finish(2, interactive)

    # 2. where results go, and what is already done
    lengths = sorted(set(args.gen or []))
    if args.out:
        out = Path(args.out)
    elif gen_mode:
        tag = "-".join(f"{n}c" for n in lengths)
        out = _app_dir() / DEFAULT_OUT / f"{tag}-{args.charset}"
    else:
        out = (_app_dir() if interactive else Path.cwd()) / DEFAULT_OUT
    save = not args.no_save
    resume = args.resume or (gen_mode and not args.fresh)
    done = load_done(out) if (save and resume) else set()

    # 3. names to check (lazy for --gen: 4c alone is ~1.7 million names)
    valid, invalid = runner.prepare(runner.parse_list(text)) if text else ([], [])
    total = len(valid) + len(invalid)
    names: Iterable[str] = valid
    if gen_mode:
        names = itertools.chain(valid, *(
            generate.combos(n, args.charset, shuffle=args.shuffle or n >= 4) for n in lengths))
        total += sum(generate.total(n, args.charset) for n in lengths)
    if done:
        invalid = [r for r in invalid if r.username not in done]
        names = (n for n in names if n not in done)
        total = max(0, total - len(done))
    if args.limit is not None:
        names = itertools.islice(names, args.limit)
        total = min(total, args.limit + len(invalid))

    if total == 0:
        if done:
            print(f"Nothing left to check: every name is already in {out}")
            return _finish(0, interactive)
        print("dcheck: error: no usernames found in input", file=sys.stderr)
        return _finish(2, interactive)

    if gen_mode:
        label = ", ".join(f"{n}c" for n in lengths)
        print(f"Checking every {label} name made of '{generate.CHARSETS[args.charset]}'.")
        if done:
            print(f"Resuming: {len(done)} names already checked in {out}")
        print(f"{total} names to go - at least {_fmt_duration(total * max(args.delay, 0.05))}"
              f" at {args.delay}s per request (rate limits can make it slower).")
        print("Free names are printed and saved as they are found. Ctrl+C to stop "
              "(run again to continue).\n")

    # 4. run
    color = _use_color(args)
    lock = threading.Lock()
    counts = {s: 0 for s in Status}
    keep = bool(args.json or args.csv)
    collected: list[Result] = []
    writer = ResultWriter(out, resume=resume) if save else None
    n_done = 0
    started = time.monotonic()

    def report(r: Result) -> None:
        nonlocal n_done
        with lock:
            n_done += 1
            counts[r.status] += 1
            if keep:
                collected.append(r)
            if writer is not None:
                writer.write(r)
            if gen_mode and r.status in (Status.TAKEN, Status.INVALID):
                # 4c means ~a million "taken" lines; show a progress line instead
                if n_done % 100 == 0 and not args.only_available:
                    rate = n_done / max(time.monotonic() - started, 1e-9)
                    print(f"  ...{n_done}/{total} checked, {counts[Status.AVAILABLE]} free "
                          f"so far, ~{_fmt_duration((total - n_done) / rate)} left",
                          flush=True)
                return
            if args.only_available and r.status is not Status.AVAILABLE:
                return
            print(_format_line(r, n_done, total, color), flush=True)

    interrupted = False
    try:
        for r in invalid:
            report(r)
        runner.run(names, make_client(args), workers=args.workers, on_result=report,
                   keep_results=False)
    except KeyboardInterrupt:
        interrupted = True
        print("\nStopped." + (" Run the same thing again to continue." if gen_mode else ""),
              file=sys.stderr)
    finally:
        if writer is not None:
            writer.close()

    try:
        _write_exports(args, collected)
    except OSError as e:
        print(f"dcheck: error: cannot write output: {e}", file=sys.stderr)
        return _finish(1, interactive)

    print(f"\nDone: {n_done} checked - {counts[Status.AVAILABLE]} available, "
          f"{counts[Status.TAKEN]} taken, {counts[Status.INVALID]} invalid, "
          f"{counts[Status.ERROR]} errors")
    if writer is not None and n_done:
        msg = f"Results saved to: {out.resolve()}"
        if counts[Status.AVAILABLE]:
            msg += f"  (free names are in {_OUT_FILES[Status.AVAILABLE]})"
        print(msg)
    return _finish(130 if interrupted else 0, interactive)
