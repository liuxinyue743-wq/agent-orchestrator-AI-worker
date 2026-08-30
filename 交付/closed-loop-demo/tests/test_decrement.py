from app import decrement


def test_decrement_basic():
    assert decrement(3) == 2


def test_decrement_zero():
    assert decrement(0) == -1


def test_decrement_negative():
    assert decrement(-5) == -6
