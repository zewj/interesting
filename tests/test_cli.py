import csv
import io
import json
import re
import sys

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


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    # results/ is written to the current directory by default
    monkeypatch.chdir(tmp_path)


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


def test_no_input_exits_2(monkeypatch, fake, no_stdin, capsys):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
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
    last = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("Done:"))
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


def test_no_input_no_interactive_exits_2(fake, no_stdin, capsys):
    assert cli.main(["--no-interactive"]) == 2
    assert fake.checked == []


def test_interactive_pasted_names(monkeypatch, tmp_path, fake, no_stdin, capsys):
    monkeypatch.chdir(tmp_path)
    answers = iter(["1", "avone, takenone", "avtwo", "", ""])  # menu, names, blank, Enter
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main([]) == 0
    assert fake.checked == ["avone", "takenone", "avtwo"]
    assert (tmp_path / "results" / "available.txt").read_text().split() == ["avone", "avtwo"]
    assert "Results saved to" in capsys.readouterr().out


def test_interactive_dragged_file(monkeypatch, tmp_path, fake, no_stdin, capsys):
    monkeypatch.chdir(tmp_path)
    f = write(tmp_path, "list.txt", "avone\ntakenone\n")
    answers = iter(["1", f'"{f}"', "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main([]) == 0
    assert fake.checked == ["avone", "takenone"]


def test_interactive_nothing_entered(monkeypatch, fake, no_stdin, capsys):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert cli.main([]) == 2
    assert fake.checked == []


def test_default_usernames_txt_from_menu(monkeypatch, tmp_path, fake, no_stdin, capsys):
    write(tmp_path, "usernames.txt", "avone\ntakenone\n")
    answers = iter(["1", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main([]) == 0
    assert fake.checked == ["avone", "takenone"]
    assert (tmp_path / "results" / "available.txt").read_text() == "avone\n"


def test_default_usernames_txt_non_interactive(tmp_path, fake, no_stdin, capsys):
    write(tmp_path, "usernames.txt", "avone\n")
    assert cli.main(["--no-interactive"]) == 0
    assert fake.checked == ["avone"]


def test_results_saved_by_default(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone takenone\n")
    assert cli.main([f]) == 0
    assert (tmp_path / "results" / "available.txt").read_text() == "avone\n"
    assert (tmp_path / "results" / "taken.txt").read_text() == "takenone\n"


def test_no_save(tmp_path, fake, no_stdin, capsys):
    f = write(tmp_path, "n.txt", "avone\n")
    assert cli.main([f, "--no-save"]) == 0
    assert not (tmp_path / "results").exists()


def test_new_run_replaces_old_results(tmp_path, fake, no_stdin, capsys):
    out = tmp_path / "results"
    out.mkdir()
    (out / "available.txt").write_text("stale\n")
    f = write(tmp_path, "n.txt", "avone\n")
    assert cli.main([f]) == 0
    assert (out / "available.txt").read_text() == "avone\n"


def test_resume_skips_done_names(tmp_path, fake, no_stdin, capsys):
    out = tmp_path / "results"
    out.mkdir()
    (out / "taken.txt").write_text("takenone\n")
    (out / "errors.txt").write_text("errold\n")
    f = write(tmp_path, "n.txt", "takenone avone\n")
    assert cli.main([f, "--resume"]) == 0
    assert fake.checked == ["avone"]
    assert (out / "taken.txt").read_text() == "takenone\n"
    assert not (out / "errors.txt").exists()  # errors are retried, so the old file is reset


def test_gen_2c_digits(tmp_path, fake, no_stdin, capsys):
    assert cli.main(["--gen", "2", "--charset", "digits", "-w", "2"]) == 0
    assert sorted(fake.checked) == [f"{a}{b}" for a in "0123456789" for b in "0123456789"]
    out = tmp_path / "results" / "2c-digits"
    assert len((out / "taken.txt").read_text().split()) == 100
    text = capsys.readouterr().out
    assert "...100/100 checked" in text  # progress line instead of 100 TAKEN lines
    assert "[TAKEN]" not in text


def test_gen_resumes_automatically(tmp_path, fake, no_stdin, capsys):
    assert cli.main(["--gen", "2", "--charset", "digits", "--limit", "30"]) == 0
    first = list(fake.checked)
    assert len(first) == 30
    fake.checked.clear()
    assert cli.main(["--gen", "2", "--charset", "digits"]) == 0
    assert len(fake.checked) == 70
    assert not set(first) & set(fake.checked)
    fake.checked.clear()
    assert cli.main(["--gen", "2", "--charset", "digits"]) == 0
    assert fake.checked == []
    assert "Nothing left to check" in capsys.readouterr().out


def test_gen_fresh_starts_over(tmp_path, fake, no_stdin, capsys):
    assert cli.main(["--gen", "2", "--charset", "digits", "--limit", "10"]) == 0
    fake.checked.clear()
    assert cli.main(["--gen", "2", "--charset", "digits", "--fresh", "--limit", "10"]) == 0
    assert len(fake.checked) == 10


def test_gen_from_menu(monkeypatch, tmp_path, fake, no_stdin, capsys):
    answers = iter(["2", "", ""])  # 2c, no _/., Enter to exit
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli.generate, "CHARSETS", {**cli.generate.CHARSETS, "alnum": "av"})
    assert cli.main([]) == 0
    assert sorted(fake.checked) == ["aa", "av", "va", "vv"]
    assert (tmp_path / "results" / "2c-alnum" / "available.txt").read_text().split() == ["av"]


def test_ctrl_c_keeps_found_names(tmp_path, monkeypatch, no_stdin, capsys):
    class AlwaysFree:
        def check(self, name):
            return Result(name, Status.AVAILABLE)

    def fake_run(names, client, *, workers, on_result, keep_results):
        it = iter(names)
        on_result(client.check(next(it)))
        on_result(client.check(next(it)))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "make_client", lambda args: AlwaysFree())
    monkeypatch.setattr(cli.runner, "run", fake_run)
    f = write(tmp_path, "n.txt", "one two three four\n")
    assert cli.main([f]) == 130
    assert (tmp_path / "results" / "available.txt").read_text().split() == ["one", "two"]
