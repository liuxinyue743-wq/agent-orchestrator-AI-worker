from math2 import cube


def test_cube_positive():
    assert cube(2) == 8


def test_cube_negative():
    assert cube(-3) == -27
