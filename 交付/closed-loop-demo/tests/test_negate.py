from app import negate


def test_negate_positive():
    assert negate(5) == -5


def test_negate_zero():
    assert negate(0) == 0


def test_negate_negative():
    assert negate(-3) == 3
