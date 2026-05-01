"""City lookup: highest-population fallback + country-code disambiguation."""

from __future__ import annotations

import dataclasses

import pytest

from trip_tracker.geo.cities import lookup_city


def test_lookup_paris_returns_paris_france() -> None:
    """No country hint → highest-pop match (Paris/FR ~2.1M)."""
    c = lookup_city("Paris")
    assert c is not None
    assert c.country_code == "FR"
    assert c.population > 2_000_000


def test_lookup_paris_with_us_country_returns_paris_us() -> None:
    c = lookup_city("Paris", country="US")
    assert c is not None
    assert c.country_code == "US"
    # Paris, Texas / Paris, KY etc. are all <100k
    assert c.population < 100_000


def test_lookup_unknown_city_returns_none() -> None:
    assert lookup_city("Xyzzyglop") is None


def test_lookup_handles_diacritics_via_asciiname() -> None:
    """'Zuerich' (umlaut expanded to 'ue') should match 'Zürich' via the asciiname index."""
    c = lookup_city("Zuerich")
    assert c is not None
    assert c.country_code == "CH"


def test_city_is_frozen_dataclass() -> None:
    c = lookup_city("Paris")
    assert c is not None
    assert dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.population = 0  # type: ignore[misc]
