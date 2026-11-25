"""Input parsing, preparation and concurrent execution of username checks."""
from __future__ import annotations

import re
import threading
from typing import Callable, Iterable

from . import validate
from .models import Result, Status

_SPLIT_RE = re.compile(r"[,\s]+")


def parse_list(text: str) -> list[str]:
    """Split text on newlines, commas and whitespace; drop blanks and `#` comments."""
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        names.extend(tok for tok in _SPLIT_RE.split(line) if tok)
    return names


def prepare(names: Iterable[str]) -> tuple[list[str], list[Result]]:
    """Normalize, dedupe (first-seen order) and validate names.

    Returns (valid_names_to_check, invalid_results).
    """
    seen: set[str] = set()
    valid: list[str] = []
    invalid: list[Result] = []
    for raw in names:
        name = validate.normalize(raw)
        if name in seen:
            continue
        seen.add(name)
        reason = validate.validate(name)
        if reason is None:
            valid.append(name)
        else:
            invalid.append(Result(name, Status.INVALID, reason))
    return valid, invalid


def run(
    names: Iterable[str],
    client,
    *,
    workers: int = 3,
    on_result: Callable[[Result], None] | None = None,
    keep_results: bool = True,
) -> list[Result]:
    """Check every name with ``client.check`` concurrently.

    ``names`` may be a lazy iterator (e.g. millions of generated names): worker threads
    pull from it one at a time, so nothing is queued up front. ``on_result`` is called
    (serialized by a lock) as each check finishes. Returns results in input order, or
    ``[]`` when ``keep_results`` is False. On Ctrl+C, no further ``on_result`` calls are
    made and ``KeyboardInterrupt`` is re-raised; anything already reported is kept by the
    caller. If ``on_result`` raises, the run stops and that exception is re-raised.
    """
    source = iter(enumerate(names))
    src_lock = threading.Lock()
    cb_lock = threading.Lock()
    stop = threading.Event()
    results: dict[int, Result] = {}
    failure: list[BaseException] = []

    def _check(name: str) -> Result:
        try:
            return client.check(name)
        except Exception as exc:  # noqa: BLE001 - any failure becomes an ERROR result
            return Result(name, Status.ERROR, str(exc))

    def _worker() -> None:
        while not stop.is_set():
            with src_lock:
                try:
                    i, name = next(source)
                except StopIteration:
                    return
            res = _check(name)
            if stop.is_set():
                return
            with cb_lock:
                if stop.is_set():
                    return
                if keep_results:
                    results[i] = res
                if on_result is not None:
                    try:
                        on_result(res)
                    except BaseException as exc:  # e.g. a result file can't be written
                        failure.append(exc)
                        stop.set()
                        return

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.2)  # short joins keep the main thread responsive to Ctrl+C
    except KeyboardInterrupt:
        with cb_lock:  # wait for any in-flight on_result call; none start after this
            stop.set()
        raise
    if failure:
        raise failure[0]
    return [results[i] for i in sorted(results)]


def summarize(results: Iterable[Result]) -> dict[str, int]:
    """Count results per status value; always includes all statuses."""
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[Status(r.status).value] += 1
    return counts
