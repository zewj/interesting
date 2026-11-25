"""Input parsing, preparation and concurrent execution of username checks."""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Sequence

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
    names: Sequence[str],
    client,
    *,
    workers: int = 3,
    on_result: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Check every name with ``client.check`` concurrently; return results in input order."""
    names = list(names)
    results: list[Result | None] = [None] * len(names)
    if not names:
        return []
    lock = threading.Lock()

    def _check(name: str) -> Result:
        try:
            return client.check(name)
        except Exception as exc:  # noqa: BLE001 - any failure becomes an ERROR result
            return Result(name, Status.ERROR, str(exc))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_check, name): i for i, name in enumerate(names)}
        for fut in as_completed(futures):
            res = fut.result()
            results[futures[fut]] = res
            if on_result is not None:
                with lock:
                    on_result(res)
    return results  # type: ignore[return-value]


def summarize(results: Iterable[Result]) -> dict[str, int]:
    """Count results per status value; always includes all statuses."""
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[Status(r.status).value] += 1
    return counts
