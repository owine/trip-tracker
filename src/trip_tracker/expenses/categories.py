"""Expense category enum + display labels. Spec §4.5."""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    FOOD = "food"
    TRANSIT = "transit"
    LODGING = "lodging"
    ACTIVITIES = "activities"
    SHOPPING = "shopping"
    GRATUITIES = "gratuities"
    CONNECTIVITY = "connectivity"
    OTHER = "other"


CATEGORY_LABELS: dict[Category, str] = {
    Category.FOOD: "Food",
    Category.TRANSIT: "Transit",
    Category.LODGING: "Lodging",
    Category.ACTIVITIES: "Activities",
    Category.SHOPPING: "Shopping",
    Category.GRATUITIES: "Gratuities",
    Category.CONNECTIVITY: "Connectivity",
    Category.OTHER: "Other",
}
