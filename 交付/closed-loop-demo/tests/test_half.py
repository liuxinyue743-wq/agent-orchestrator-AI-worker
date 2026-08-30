from math2 import half


def test_half_ten():
    assert half(10) == 5


def test_half_odd():
    assert half(7) == 3.5


def test_half_zero():
    assert half(0) == 0
