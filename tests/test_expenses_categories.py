"""Category enum + display labels."""

from __future__ import annotations

from trip_tracker.expenses.categories import CATEGORY_LABELS, Category


def test_category_values_are_lowercase_snake() -> None:
    expected = {
        "food",
        "transit",
        "lodging",
        "activities",
        "shopping",
        "gratuities",
        "connectivity",
        "other",
    }
    assert {c.value for c in Category} == expected


def test_category_labels_cover_all_values() -> None:
    assert set(CATEGORY_LABELS) == set(Category)


def test_category_label_examples() -> None:
    assert CATEGORY_LABELS[Category.FOOD] == "Food"
    assert CATEGORY_LABELS[Category.CONNECTIVITY] == "Connectivity"
