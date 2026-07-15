from pricing import calculate_discounted_total


def test_calculate_discounted_total() -> None:
    assert calculate_discounted_total(100, 20) == 80
