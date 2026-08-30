import pytest


def test_divide_normal():
    from app import divide
    assert divide(6, 3) == 2


def test_divide_by_zero():
    from app import divide
    with pytest.raises(ValueError):
        divide(1, 0)
