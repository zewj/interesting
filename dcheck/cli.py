"""Command-line interface: ``python -m dcheck [FILES...]``."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from pathlib import Path
from typing import Sequence

from . import runner
from .models import Result, Status

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dcheck",
        description="Bulk-check Discord username availability.",
    )
    p.add_argument("files", nargs="*", metavar="FILE",
                   help="files with usernames (newline/comma/space separated, # comments); "
                        "'-' or no files reads stdin")
    p.add_argument("-w", "--workers", type=int, default=3,
                   help="number of concurrent worker threads (default: 3)")
    p.add_argument("--delay", type=float, default=0.5,
                   help="minimum seconds between requests across all workers (default: 0.5)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="HTTP request timeout in seconds (default: 10)")
    p.add_argument("-o", "--out", metavar="DIR",
                   help="write available.txt, taken.txt, invalid.txt, errors.txt into DIR")
    p.add_argument("--json", metavar="FILE", help="write all results as JSON to FILE")
    p.add_argument("--csv", metavar="FILE", help="write all results as CSV to FILE")
    p.add_argument("--only-available", action="store_true",
                   help="only print available usernames to the console")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--web", action="store_true", help="start the local web UI instead")
    p.add_argument("--host", default="127.0.0.1", help="web UI host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="web UI port (default: 8765)")
    return p


def make_client(args: argparse.Namespace):
    """Create the username-checking client. Patched in tests."""
    from .client import DiscordClient

    return DiscordClient(timeout=args.timeout, min_interval=args.delay)


def _read_input(files: Sequence[str]) -> str | None:
    """Return the combined input text, or None if there is no input source."""
    if not files:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return sys.stdin.read()
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


def _write_outputs(args: argparse.Namespace, results: list[Result]) -> None:
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for status, fname in _OUT_FILES.items():
            names = [r.username for r in results if r.status is status]
            if names:
                (out / fname).write_text("\n".join(names) + "\n", encoding="utf-8")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # --help or argparse usage error
        return e.code if isinstance(e.code, int) else 2

    if args.workers < 1:
        parser.print_usage(sys.stderr)
        print("dcheck: error: --workers must be at least 1", file=sys.stderr)
        return 2
    if args.delay < 0 or args.timeout <= 0:
        parser.print_usage(sys.stderr)
        print("dcheck: error: --delay must be >= 0 and --timeout > 0", file=sys.stderr)
        return 2

    if args.web:
        from . import web

        web.serve(args.host, args.port, workers=args.workers,
                  min_interval=args.delay, timeout=args.timeout)
        return 0

    try:
        text = _read_input(args.files)
    except OSError as e:
        print(f"dcheck: error: cannot read input: {e}", file=sys.stderr)
        return 2
    if text is None:
        parser.print_usage(sys.stderr)
        print("dcheck: error: no input given (pass FILEs, '-' or pipe usernames on stdin)",
              file=sys.stderr)
        return 2

    valid, invalid = runner.prepare(runner.parse_list(text))
    total = len(valid) + len(invalid)
    if total == 0:
        print("dcheck: error: no usernames found in input", file=sys.stderr)
        return 2

    color = _use_color(args)
    lock = threading.Lock()
    done = 0

    def report(r: Result) -> None:
        nonlocal done
        with lock:
            done += 1
            if args.only_available and r.status is not Status.AVAILABLE:
                return
            print(_format_line(r, done, total, color), flush=True)

    for r in invalid:
        report(r)

    interrupted = False
    checked: list[Result] = []
    if valid:
        try:
            checked = runner.run(valid, make_client(args), workers=args.workers,
                                 on_result=report)
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrupted.", file=sys.stderr)

    results = list(invalid) + list(checked)
    try:
        _write_outputs(args, results)
    except OSError as e:
        print(f"dcheck: error: cannot write output: {e}", file=sys.stderr)
        return 1

    if not interrupted:
        s = runner.summarize(results)
        print(f"Done: {total} checked - {s['available']} available, {s['taken']} taken, "
              f"{s['invalid']} invalid, {s['error']} errors")
    return 130 if interrupted else 0
