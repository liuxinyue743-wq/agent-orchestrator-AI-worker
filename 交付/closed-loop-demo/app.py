def add(a: float, b: float) -> float:
    return a + b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def square(a):
    return a * a


def decrement(a):
    return a - 1


def negate(a):
    return -a
