"""HTTP client for Discord's public, unauthenticated username-availability check.

The client is thread-safe: several worker threads may call :meth:`DiscordClient.check`
concurrently. All requests share one pacing schedule (at least ``min_interval`` seconds
between request starts across every thread) and one "blocked until" deadline that a 429
response sets, so a rate limit pauses every thread, not just the one that hit it.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import __version__
from .models import Result, Status

API_URL = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
USER_AGENT = f"Mozilla/5.0 (compatible; dcheck/{__version__}; +username-availability-checker)"

DEFAULT_RETRY_AFTER = 5.0  # seconds, when a 429 gives no usable hint
BACKOFF_BASE = 1.0         # seconds; doubled per failed attempt
BACKOFF_MAX = 30.0
DETAIL_MAX = 200

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    """POST ``body`` to ``url``. Never raises on HTTP error codes; network errors propagate."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        hdrs = dict(e.headers.items()) if e.headers is not None else {}
        return e.code, hdrs, data or b""


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    lname = name.lower()
    for k, v in headers.items():
        if str(k).lower() == lname:
            return v
    return None


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _shorten(text: str, limit: int = DETAIL_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_error_message(errors: Any) -> str | None:
    """Depth-first search for the first ``_errors[...].message`` in a Discord errors tree."""
    if isinstance(errors, dict):
        errs = errors.get("_errors")
        if isinstance(errs, list):
            for item in errs:
                if isinstance(item, dict) and item.get("message"):
                    return str(item["message"])
        for key, value in errors.items():
            if key == "_errors":
                continue
            found = _first_error_message(value)
            if found:
                return found
    elif isinstance(errors, list):
        for value in errors:
            found = _first_error_message(value)
            if found:
                return found
    return None


def _invalid_detail(body: bytes) -> str:
    data = _parse_json(body)
    if isinstance(data, dict):
        msg = _first_error_message(data.get("errors"))
        if msg:
            return _shorten(msg)
        if data.get("message"):
            return _shorten(str(data["message"]))
    text = body.decode("utf-8", "replace").strip() if body else ""
    return _shorten(text) if text else "rejected by Discord (HTTP 400)"


def _retry_after(headers: dict[str, str], body: bytes) -> float:
    data = _parse_json(body)
    if isinstance(data, dict) and "retry_after" in data:
        try:
            value = float(data["retry_after"])
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    hdr = _header(headers, "Retry-After")
    if hdr is not None:
        try:
            value = float(str(hdr).strip())
            if value >= 0:
                return value
        except ValueError:
            pass
    return DEFAULT_RETRY_AFTER


class DiscordClient:
    def __init__(self, *, timeout: float = 10.0, max_retries: int = 5, min_interval: float = 0.5,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.min_interval = max(0.0, float(min_interval))
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        # _pace_lock serializes slot acquisition (held while waiting, so waiters queue up);
        # _state_lock guards the shared timestamps and is never held while sleeping.
        self._pace_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_allowed = float("-inf")
        self._blocked_until = float("-inf")

    # -- shared rate-limit state -------------------------------------------------
    def _wait_for_slot(self) -> None:
        with self._pace_lock:
            while True:
                now = self._clock()
                with self._state_lock:
                    target = max(self._next_allowed, self._blocked_until)
                    if target <= now:
                        self._next_allowed = now + self.min_interval
                        return
                self._sleep(target - now)

    def _block_for(self, seconds: float) -> None:
        with self._state_lock:
            deadline = self._clock() + seconds
            if deadline > self._blocked_until:
                self._blocked_until = deadline

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX))

    # -- public API --------------------------------------------------------------
    def check(self, username: str) -> Result:
        try:
            return self._check(username)
        except Exception as exc:  # never raise from check()
            return Result(username, Status.ERROR, _shorten(f"{type(exc).__name__}: {exc}"))

    def _check(self, username: str) -> Result:
        body = json.dumps({"username": username}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        last_detail = "unknown error"
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            is_last = attempt == attempts - 1
            self._wait_for_slot()
            try:
                status, resp_headers, resp_body = self._transport(API_URL, body, headers, self.timeout)
            except urllib.error.URLError as exc:
                last_detail = _shorten(f"network error: {exc.reason}")
            except Exception as exc:  # timeouts, connection resets, ...
                last_detail = _shorten(f"network error: {type(exc).__name__}: {exc}")
            else:
                resp_headers = resp_headers or {}
                resp_body = resp_body or b""
                if status == 200:
                    data = _parse_json(resp_body)
                    taken = data.get("taken") if isinstance(data, dict) else None
                    if isinstance(taken, bool):
                        return Result(username, Status.TAKEN if taken else Status.AVAILABLE)
                    last_detail = "unexpected response: " + _shorten(
                        resp_body.decode("utf-8", "replace") or "<empty>", 100)
                elif status == 400:
                    return Result(username, Status.INVALID, _invalid_detail(resp_body))
                elif status == 429:
                    last_detail = "rate limited"
                    self._block_for(_retry_after(resp_headers, resp_body))
                    continue  # the shared deadline already provides the wait
                elif status in (401, 403, 404):
                    # Blocked (e.g. Cloudflare/IP ban) or endpoint gone: retrying won't help.
                    return Result(username, Status.ERROR, f"HTTP {status} (blocked or endpoint unavailable)")
                else:
                    last_detail = f"HTTP {status}"
            if not is_last:
                self._backoff(attempt)
        return Result(username, Status.ERROR, last_detail)
