import random
import threading
import time

import pytest

from dcheck import runner
from dcheck.models import Result, Status


@pytest.fixture
def fake_validate(monkeypatch):
    """Replace validate.normalize/validate with simple local stand-ins."""

    def normalize(name):
        name = name.strip()
        if name.startswith("@"):
            name = name[1:]
        return name.lower()

    def validate(name):
        if len(name) < 2:
            return "too short"
        if ".." in name:
            return "consecutive periods"
        return None

    monkeypatch.setattr(runner.validate, "normalize", normalize, raising=False)
    monkeypatch.setattr(runner.validate, "validate", validate, raising=False)


class FakeClient:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []
        self.lock = threading.Lock()

    def check(self, name):
        time.sleep(random.uniform(0, 0.01))
        with self.lock:
            self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"boom {name}")
        status = Status.TAKEN if len(name) % 2 else Status.AVAILABLE
        return Result(name, status)


# parse_list -----------------------------------------------------------------

def test_parse_list_mixed_separators_comments_blanks():
    text = (
        "alice, bob\tcarol\n"
        "\n"
        "   \n"
        "# full-line comment\n"
        "dave # trailing comment, ignored\n"
        ",,eve,,  frank\r\n"
        "grace#nospace\n"
    )
    assert runner.parse_list(text) == [
        "alice", "bob", "carol", "dave", "eve", "frank", "grace",
    ]


def test_parse_list_empty():
    assert runner.parse_list("") == []
    assert runner.parse_list("\n\n# only comments\n , ,\n") == []


# prepare --------------------------------------------------------------------

def test_prepare_dedupes_preserving_order_and_splits_invalid(fake_validate):
    names = ["Bob", "@alice", "x", "bob", "a..b", "ALICE", "carol", "x"]
    valid, invalid = runner.prepare(names)
    assert valid == ["bob", "alice", "carol"]
    assert [r.username for r in invalid] == ["x", "a..b"]
    assert all(r.status is Status.INVALID for r in invalid)
    assert all(r.detail for r in invalid)


def test_prepare_accepts_generator(fake_validate):
    valid, invalid = runner.prepare(n for n in ["aa", "bb", "aa"])
    assert valid == ["aa", "bb"]
    assert invalid == []


# run ------------------------------------------------------------------------

@pytest.mark.parametrize("workers", [1, 3, 8])
def test_run_preserves_input_order(workers):
    names = [f"user{i}" + "x" * (i % 3) for i in range(40)]
    client = FakeClient()
    seen = []
    results = runner.run(names, client, workers=workers, on_result=seen.append)
    assert [r.username for r in results] == names
    assert sorted(client.calls) == sorted(names)
    # on_result called exactly once per name
    assert len(seen) == len(names)
    assert sorted(r.username for r in seen) == sorted(names)


def test_run_exception_becomes_error_result():
    names = ["good", "bad", "fine"]
    client = FakeClient(fail={"bad"})
    results = runner.run(names, client, workers=2)
    assert [r.username for r in results] == names
    assert results[1] == Result("bad", Status.ERROR, "boom bad")
    assert results[0].status is not Status.ERROR
    assert results[2].status is not Status.ERROR


def test_run_on_result_calls_are_serialized():
    active = 0
    max_active = 0
    guard = threading.Lock()

    def on_result(_res):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.002)
        with guard:
            active -= 1

    names = [f"n{i:02d}" for i in range(20)]
    runner.run(names, FakeClient(), workers=5, on_result=on_result)
    assert max_active == 1


def test_run_empty():
    assert runner.run([], FakeClient()) == []


# summarize ------------------------------------------------------------------

def test_summarize_has_all_keys():
    assert runner.summarize([]) == {
        "available": 0, "taken": 0, "invalid": 0, "error": 0,
    }
    results = [
        Result("a", Status.AVAILABLE),
        Result("b", Status.AVAILABLE),
        Result("c", Status.TAKEN),
        Result("d", Status.ERROR, "x"),
    ]
    assert runner.summarize(results) == {
        "available": 2, "taken": 1, "invalid": 0, "error": 1,
    }
