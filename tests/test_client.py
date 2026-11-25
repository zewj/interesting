import io
import json
import threading
import urllib.error

import pytest

from dcheck import client as client_mod
from dcheck.client import API_URL, DiscordClient, urllib_transport
from dcheck.models import Result, Status


class FakeTime:
    """Thread-safe fake clock; sleep() advances it instantly."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []
        self.lock = threading.Lock()

    def clock(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            if seconds > 0:
                self.now += seconds


class FakeTransport:
    """Returns scripted responses in order; an Exception instance is raised instead."""

    def __init__(self, responses, ft=None):
        self.responses = list(responses)
        self.calls = []
        self.ft = ft
        self.lock = threading.Lock()

    def __call__(self, url, body, headers, timeout):
        with self.lock:
            self.calls.append({
                "url": url, "body": json.loads(body), "headers": headers, "timeout": timeout,
                "t": self.ft.clock() if self.ft else None,
            })
            resp = self.responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def js(status, obj, headers=None):
    return (status, headers or {}, json.dumps(obj).encode())


def make(responses, **kw):
    ft = FakeTime()
    tr = FakeTransport(responses, ft)
    kw.setdefault("min_interval", 0.5)
    c = DiscordClient(transport=tr, sleep=ft.sleep, clock=ft.clock, **kw)
    return c, tr, ft


def test_taken():
    c, tr, _ = make([js(200, {"taken": True})])
    assert c.check("alice") == Result("alice", Status.TAKEN)
    call = tr.calls[0]
    assert call["url"] == API_URL
    assert call["body"] == {"username": "alice"}
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"]
    assert call["timeout"] == 10.0


def test_available():
    c, _, _ = make([js(200, {"taken": False})])
    assert c.check("bob") == Result("bob", Status.AVAILABLE)


def test_400_invalid_uses_nested_error_message():
    body = {
        "message": "Invalid Form Body",
        "code": 50035,
        "errors": {"username": {"_errors": [
            {"code": "USERNAME_INVALID_CONTAINS", "message": "Username cannot contain \"discord\""},
        ]}},
    }
    c, tr, _ = make([js(400, body)])
    r = c.check("discordfan")
    assert r.status is Status.INVALID
    assert r.detail == 'Username cannot contain "discord"'
    assert len(tr.calls) == 1


def test_400_invalid_falls_back_to_message_and_text():
    c, _, _ = make([js(400, {"message": "Invalid Form Body"}), (400, {}, b"nope"), (400, {}, b"")])
    assert c.check("a").detail == "Invalid Form Body"
    assert c.check("b").detail == "nope"
    r = c.check("c")
    assert r.status is Status.INVALID and r.detail


def test_429_json_retry_after_then_success():
    c, tr, ft = make([js(429, {"retry_after": 3.25, "global": False}), js(200, {"taken": False})])
    r = c.check("carol")
    assert r == Result("carol", Status.AVAILABLE)
    assert len(tr.calls) == 2
    assert tr.calls[1]["t"] - tr.calls[0]["t"] >= 3.25
    assert pytest.approx(3.25) in ft.sleeps


def test_429_header_only():
    c, tr, ft = make([(429, {"retry-after": "7"}, b""), js(200, {"taken": True})])
    assert c.check("dave").status is Status.TAKEN
    assert tr.calls[1]["t"] - tr.calls[0]["t"] >= 7
    assert 7 in ft.sleeps


def test_429_non_json_body_uses_header_then_default():
    html = b"<html>Cloudflare: you are being rate limited</html>"
    c, tr, _ = make([(429, {"Retry-After": "2"}, html), js(200, {"taken": True})])
    assert c.check("erin").status is Status.TAKEN
    assert tr.calls[1]["t"] - tr.calls[0]["t"] >= 2

    c, tr, _ = make([(429, {}, html), js(200, {"taken": True})])
    assert c.check("erin").status is Status.TAKEN
    assert tr.calls[1]["t"] - tr.calls[0]["t"] >= client_mod.DEFAULT_RETRY_AFTER


def test_429_blocks_all_threads_shared_deadline():
    # One 429 sets the shared deadline; the next request (for another name) also waits for it.
    c, tr, _ = make([js(429, {"retry_after": 10}), js(200, {"taken": True}), js(200, {"taken": False})],
                    min_interval=0)
    assert c.check("x").status is Status.TAKEN
    c._block_for(20)
    assert c.check("y").status is Status.AVAILABLE
    assert tr.calls[2]["t"] - tr.calls[1]["t"] >= 20


def test_rate_limited_retries_exhausted():
    c, tr, _ = make([js(429, {"retry_after": 1})] * 3, max_retries=2)
    r = c.check("frank")
    assert r == Result("frank", Status.ERROR, "rate limited")
    assert len(tr.calls) == 3


def test_5xx_retries_exhausted():
    c, tr, ft = make([(503, {}, b"down")] * 4, max_retries=3)
    r = c.check("gina")
    assert r.status is Status.ERROR
    assert "503" in r.detail
    assert len(tr.calls) == 4
    backoffs = [s for s in ft.sleeps if s >= 1]
    assert backoffs == [1.0, 2.0, 4.0]  # exponential, no sleep after the final attempt


def test_transport_raises_urlerror():
    c, tr, _ = make([urllib.error.URLError("Name or service not known")] * 2, max_retries=1)
    r = c.check("hank")
    assert r.status is Status.ERROR
    assert "Name or service not known" in r.detail
    assert len(tr.calls) == 2


def test_urlerror_then_success():
    c, _, _ = make([TimeoutError("timed out"), js(200, {"taken": False})])
    assert c.check("ivy").status is Status.AVAILABLE


def test_500_then_success():
    c, tr, ft = make([(500, {}, b"oops"), js(200, {"taken": True})])
    assert c.check("jack").status is Status.TAKEN
    assert len(tr.calls) == 2
    assert 1.0 in ft.sleeps


def test_200_garbage_is_retried():
    c, tr, _ = make([(200, {}, b"not json"), js(200, {"foo": 1}), js(200, {"taken": False})])
    assert c.check("kim").status is Status.AVAILABLE
    assert len(tr.calls) == 3


def test_check_never_raises_even_if_transport_raises_weird_things():
    c, _, _ = make([RuntimeError("boom")], max_retries=0)
    r = c.check("lee")
    assert r.status is Status.ERROR and "boom" in r.detail

    def bad_sleep(_):
        raise ValueError("sleep broke")

    ft = FakeTime()
    c = DiscordClient(transport=FakeTransport([(500, {}, b"")] * 3), sleep=bad_sleep, clock=ft.clock)
    r = c.check("lee")
    assert r.status is Status.ERROR and "sleep broke" in r.detail


def test_pacing_spaces_requests():
    c, tr, ft = make([js(200, {"taken": False})] * 4, min_interval=0.5)
    for n in "abcd":
        c.check(n * 2)
    times = [call["t"] for call in tr.calls]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.5 - 1e-9 for g in gaps)
    assert ft.sleeps == [pytest.approx(0.5)] * 3  # first request goes immediately


def test_pacing_no_sleep_when_enough_time_passed():
    c, tr, ft = make([js(200, {"taken": False})] * 2, min_interval=0.5)
    c.check("aa")
    ft.now += 5
    c.check("bb")
    assert ft.sleeps == []


def test_concurrency_many_threads():
    names = [f"user{i}" for i in range(40)]
    ft = FakeTime()
    lock = threading.Lock()
    calls = []
    state = {"rate_limited_once": False}
    slots = []

    def transport(url, body, headers, timeout):
        name = json.loads(body)["username"]
        with lock:
            calls.append(name)
            if name == "user7" and not state["rate_limited_once"]:
                state["rate_limited_once"] = True
                return js(429, {"retry_after": 4, "global": True})
        i = int(name[4:])
        return js(200, {"taken": i % 2 == 0})

    c = DiscordClient(transport=transport, sleep=ft.sleep, clock=ft.clock, min_interval=0.5)
    # Re-entrant pacing lock lets us record each reserved slot atomically with its reservation.
    c._pace_lock = threading.RLock()
    orig = c._wait_for_slot

    def recording_wait():
        with c._pace_lock:
            orig()
            slots.append(c._next_allowed - c.min_interval)

    c._wait_for_slot = recording_wait

    results = {}
    errors = []

    def worker(chunk):
        try:
            for n in chunk:
                results[n] = c.check(n)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(names[i::5],)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert len(results) == 40
    for n in names:
        i = int(n[4:])
        assert results[n] == Result(n, Status.TAKEN if i % 2 == 0 else Status.AVAILABLE)
    assert len(calls) == 41  # one retry for the 429
    slots.sort()
    gaps = [b - a for a, b in zip(slots, slots[1:])]
    assert all(g >= 0.5 - 1e-9 for g in gaps)
    # 40 spaced requests plus the shared 4s rate-limit pause
    assert ft.now - 1000.0 >= 40 * 0.5 + 4 - 0.5 - 1e-9


# -- default urllib transport -----------------------------------------------------

class _FakeResp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_urllib_transport_success(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return _FakeResp(200, {"Content-Type": "application/json"}, b'{"taken": false}')

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
    status, headers, body = urllib_transport(API_URL, b'{"username": "x"}', {"Content-Type": "application/json"}, 3.0)
    assert (status, body) == (200, b'{"taken": false}')
    assert seen["timeout"] == 3.0
    assert seen["req"].get_method() == "POST"
    assert seen["req"].data == b'{"username": "x"}'


def test_urllib_transport_http_error_does_not_raise(monkeypatch):
    import email.message

    def fake_urlopen(req, timeout):
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "9"
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs,
                                     io.BytesIO(b'{"retry_after": 9}'))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
    status, headers, body = urllib_transport(API_URL, b"{}", {}, 1.0)
    assert status == 429
    assert headers["Retry-After"] == "9"
    assert body == b'{"retry_after": 9}'


def test_urllib_transport_network_error_raises(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        urllib_transport(API_URL, b"{}", {}, 1.0)


def test_403_is_not_retried():
    import dcheck.client as c
    calls = []

    def transport(url, body, headers, timeout):
        calls.append(1)
        return 403, {}, b"<html>blocked</html>"

    client = c.DiscordClient(transport=transport, sleep=lambda s: None, clock=lambda: 0.0, min_interval=0)
    r = client.check("someone")
    assert r.status.value == "error"
    assert "403" in r.detail
    assert len(calls) == 1
