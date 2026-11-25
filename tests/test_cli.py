import csv
import io
import json
import re
import sys
import types

import pytest

from dcheck import cli
from dcheck.models import Result, Status

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class FakeClient:
    """Deterministic fake: names starting with 'av' are available, 'err' errors, else taken."""

    def __init__(self):
        self.checked = []

    def check(self, name):
        self.checked.append(name)
        if name.startswith("av"):
            return Result(name, Status.AVAILABLE)
        if name.startswith("err"):
            return Result(name, Status.ERROR, "boom")
        return Result(name, Status.TAKEN)


class TTYStringIO(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    created = []

    def make_client(args):
        created.append(args)
        return client

    monkeypatch.setattr(cli, "make_client", make_client)
    client.created = created
    return client


@pytest.fixture
def no_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", TTYStringIO(""))


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_file_input(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "names.txt", "avone\ntakenone # comment\n@AvTwo, x\n")
    assert cli.main([f, "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "[AVAILABLE] avone" in out
    assert "[AVAILABLE] avtwo" in out
    assert "[TAKEN] takenone" in out
    assert "[INVALID] x" in out
    assert "comment" not in fake.checked
    assert sorted(fake.checked) == ["avone", "avtwo", "takenone"]
    assert "(4/4)" in out


def test_stdin_input(monkeypatch, fake, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("avone takenone"))
    assert cli.main(["--no-color"]) == 0
    out = capsys.readouterr().out
    assert "[AVAILABLE] avone" in out
    assert "[TAKEN] takenone" in out


def test_multiple_files_and_dedupe(tmp_path, fake, no_stdin, capsys):
    a = write(tmp_path, "a.txt", "avone\ntakenone\n")
    b = write(tmp_path, "b.txt", "avone\ntakentwo\n")
    assert cli.main([a, b, "--no-color"]) == 0
    assert sorted(fake.checked) == ["avone", "takenone", "takentwo"]
    assert "(3/3)" in capsys.readouterr().out


def test_dash_means_stdin(tmp_path, monkeypatch, fake, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("fromstdin\n"))
    a = write(tmp_path, "a.txt", "fromfile\n")
    assert cli.main([a, "-", "--no-color"]) == 0
    assert sorted(fake.checked) == ["fromfile", "fromstdin"]


def test_no_input_exits_2(fake, no_stdin, capsys):
    assert cli.main([]) == 2
    assert "no input" in capsys.readouterr().err
    assert fake.checked == []


def test_empty_input_exits_2(monkeypatch, fake, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("  \n# only a comment\n"))
    assert cli.main([]) == 2


def test_missing_file_exits_2(tmp_path, fake, no_stdin, capsys):
    assert cli.main([str(tmp_path / "nope.txt")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_bad_args_exit_2(fake, no_stdin, capsys):
    assert cli.main(["--workers", "0", "-"]) == 2
    assert cli.main(["--bogus"]) == 2


def test_out_writes_only_nonempty_files(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone avtwo takenone x\n")
    out = tmp_path / "res" / "nested"
    assert cli.main([f, "-o", str(out)]) == 0
    assert (out / "available.txt").read_text().splitlines() == ["avone", "avtwo"]
    assert (out / "taken.txt").read_text().splitlines() == ["takenone"]
    assert (out / "invalid.txt").read_text().splitlines() == ["x"]
    assert not (out / "errors.txt").exists()


def test_json_and_csv(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone takenone errone x\n")
    jpath = tmp_path / "r.json"
    cpath = tmp_path / "r.csv"
    assert cli.main([f, "--json", str(jpath), "--csv", str(cpath)]) == 0

    data = json.loads(jpath.read_text())
    by_name = {d["username"]: d for d in data}
    assert set(by_name) == {"avone", "takenone", "errone", "x"}
    assert by_name["avone"] == {"username": "avone", "status": "available", "detail": ""}
    assert by_name["errone"]["status"] == "error"
    assert by_name["errone"]["detail"] == "boom"
    assert by_name["x"]["status"] == "invalid"
    assert by_name["x"]["detail"]

    with open(cpath, newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["username", "status", "detail"]
    assert {tuple(r) for r in rows[1:]} == {
        ("avone", "available", ""),
        ("takenone", "taken", ""),
        ("errone", "error", "boom"),
        ("x", "invalid", by_name["x"]["detail"]),
    }


def test_only_available(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone takenone errone x\n")
    assert cli.main([f, "--only-available", "--no-color"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("(")]
    assert len(lines) == 1
    assert "[AVAILABLE] avone" in lines[0]


def test_color_on_tty_and_no_color(tmp_path, monkeypatch, fake, no_stdin):
    f = write(tmp_path, "n.txt", "avone takenone\n")
    monkeypatch.delenv("NO_COLOR", raising=False)

    tty = TTYStringIO()
    monkeypatch.setattr(sys, "stdout", tty)
    assert cli.main([f]) == 0
    assert "\x1b[32m" in tty.getvalue()  # green for available
    assert "\x1b[31m" in tty.getvalue()  # red for taken

    tty2 = TTYStringIO()
    monkeypatch.setattr(sys, "stdout", tty2)
    assert cli.main([f, "--no-color"]) == 0
    assert not ANSI.search(tty2.getvalue())
    assert "[AVAILABLE] avone" in tty2.getvalue()


def test_no_color_when_not_tty(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone\n")
    assert cli.main([f]) == 0
    assert not ANSI.search(capsys.readouterr().out)


def test_summary_line(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone takenone takentwo errone x\n")
    assert cli.main([f, "--no-color"]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("Done: 5 checked")
    assert "1 available" in last
    assert "2 taken" in last
    assert "1 invalid" in last
    assert "1 errors" in last


def test_client_args(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone\n")
    assert cli.main([f, "-w", "5", "--delay", "1.5", "--timeout", "3"]) == 0
    (args,) = fake.created
    assert (args.workers, args.delay, args.timeout) == (5, 1.5, 3.0)


def test_web_calls_serve(monkeypatch, fake, no_stdin):
    calls = []
    fake_web = types.ModuleType("dcheck.web")
    fake_web.serve = lambda *a, **kw: calls.append((a, kw))
    monkeypatch.setitem(sys.modules, "dcheck.web", fake_web)
    import dcheck

    monkeypatch.setattr(dcheck, "web", fake_web, raising=False)

    rc = cli.main(["--web", "--host", "0.0.0.0", "--port", "9999",
                   "-w", "4", "--delay", "0.25", "--timeout", "7"])
    assert rc == 0
    assert calls == [(("0.0.0.0", 9999), {"workers": 4, "min_interval": 0.25, "timeout": 7.0})]
    assert fake.checked == []


def test_web_defaults(monkeypatch, fake, no_stdin):
    calls = []
    fake_web = types.ModuleType("dcheck.web")
    fake_web.serve = lambda *a, **kw: calls.append((a, kw))
    monkeypatch.setitem(sys.modules, "dcheck.web", fake_web)
    import dcheck

    monkeypatch.setattr(dcheck, "web", fake_web, raising=False)
    assert cli.main(["--web"]) == 0
    assert calls == [(("127.0.0.1", 8765), {"workers": 3, "min_interval": 0.5, "timeout": 10.0})]
