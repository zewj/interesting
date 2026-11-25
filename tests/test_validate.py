import pytest

from dcheck.validate import normalize, validate


# ---------------------------------------------------------------- normalize

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Alice", "alice"),
        ("ALICE_123", "alice_123"),
        ("@bob", "bob"),
        ("@Bob", "bob"),
        ("  carol  ", "carol"),
        ("\t@Dave\n", "dave"),
        ("@@eve", "@eve"),  # only ONE leading @ is stripped
        ("  @  frank", "  frank"),  # whitespace stripped before @, not after
        ("na@me", "na@me"),  # @ not at start is kept
        ("", ""),
        ("   ", ""),
        ("@", ""),
        ("a b", "a b"),  # interior whitespace untouched
        ("Ünïcode", "ünïcode"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_is_idempotent_for_simple_names():
    once = normalize("  @SomeUser ")
    assert normalize(once) == once


# ---------------------------------------------------------------- validate: valid

@pytest.mark.parametrize(
    "name",
    [
        "ab",  # min length 2
        "a" * 32,  # max length 32
        "alice",
        "user_name",
        "user.name",
        "a.b.c.d",  # single dots fine
        "__",
        "._",
        ".a",
        "a.",
        "_a_",
        "123",
        "0a",
        "a_b.c_d",
        "x" * 31 + ".",
    ],
)
def test_validate_accepts(name):
    assert validate(name) is None


# ---------------------------------------------------------------- validate: length

@pytest.mark.parametrize("name", ["", "a", "_", "."])
def test_too_short(name):
    reason = validate(name)
    assert reason is not None
    assert "short" in reason


@pytest.mark.parametrize("length", [33, 34, 100])
def test_too_long(length):
    reason = validate("a" * length)
    assert reason is not None
    assert "long" in reason


@pytest.mark.parametrize("length, ok", [(1, False), (2, True), (32, True), (33, False)])
def test_length_bounds(length, ok):
    assert (validate("a" * length) is None) is ok


# ---------------------------------------------------------------- validate: characters

@pytest.mark.parametrize(
    "name",
    [
        "Alice",  # uppercase not allowed in normalized input
        "a b",  # space inside
        " ab",
        "ab ",
        "a\tb",
        "ab\n",  # trailing newline must not slip through
        "@ab",
        "a-b",
        "a+b",
        "a!b",
        "a/b",
        "a,b",
        "ünï",  # unicode letters
        "café",
        "abc😀",  # emoji
        "ab​",  # zero-width space
        "ab٠",  # Arabic-Indic digit zero (unicode digit)
        "ａｂ",  # fullwidth letters
        "a#b",
    ],
)
def test_invalid_characters(name):
    reason = validate(name)
    assert reason is not None
    assert "character" in reason


def test_invalid_characters_reason_mentions_offender():
    reason = validate("ab-cd")
    assert "'-'" in reason


# ---------------------------------------------------------------- validate: consecutive periods

@pytest.mark.parametrize("name", ["..", "a..b", "ab..", "..ab", "a...b", "a..b..c"])
def test_double_period(name):
    reason = validate(name)
    assert reason is not None
    assert ".." in reason


def test_single_periods_separated_ok():
    assert validate("a.b.c") is None
    assert validate("._._.") is None


# ---------------------------------------------------------------- reasons are strings

@pytest.mark.parametrize("name", ["", "a" * 33, "a b", "a..b"])
def test_reason_is_nonempty_str(name):
    reason = validate(name)
    assert isinstance(reason, str) and reason


# ---------------------------------------------------------------- normalize + validate together

@pytest.mark.parametrize(
    "raw, valid",
    [
        ("  @CoolUser  ", True),
        ("@A", False),  # -> "a": too short
        ("@AB", True),
        ("@" + "A" * 32, True),
        ("@" + "A" * 33, False),
        ("Some User", False),
        ("@Ünï", False),
        ("Dots..Here", False),
        ("Under_Score.Dot", True),
    ],
)
def test_normalize_then_validate(raw, valid):
    assert (validate(normalize(raw)) is None) is valid
