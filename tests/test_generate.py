from dcheck import generate
from dcheck.validate import validate


def test_total():
    assert generate.total(2, "alnum") == 36 ** 2
    for n in (1, 2, 3):
        for cs in generate.CHARSETS:
            assert generate.total(n, cs) == sum(1 for _ in generate.combos(n, cs))
    assert generate.total(4, "all") == 2_080_880  # 38**4 minus names containing ".."


def test_in_space():
    assert generate.in_space("ab", [2], "alnum")
    assert not generate.in_space("a_", [2], "alnum")
    assert not generate.in_space("abc", [2], "alnum")
    assert not generate.in_space("..", [2], "all")


def test_combos_complete_and_unique():
    names = list(generate.combos(2, "digits"))
    assert len(names) == 100 == len(set(names))
    assert names[0] == "00" and names[-1] == "99"


def test_shuffle_is_a_permutation():
    plain = list(generate.combos(3, "digits"))
    shuffled = list(generate.combos(3, "digits", shuffle=True, seed=1))
    assert shuffled != plain
    assert sorted(shuffled) == sorted(plain)


def test_all_charset_skips_double_periods_and_is_valid():
    names = list(generate.combos(2, "all"))
    assert ".." not in names
    assert len(names) == 38 ** 2 - 1
    assert all(validate(n) is None for n in names)


def test_lazy_for_4c():
    it = generate.combos(4, "all", shuffle=True, seed=3)
    first = [next(it) for _ in range(5)]
    assert all(len(n) == 4 for n in first)
