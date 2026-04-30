"""Vendor parser packs. Each subpackage's __init__.py defines a VendorParser
subclass, which auto-registers via __init_subclass__ in parsers.base.

Adding a new vendor:
  1. Create a subpackage `parsers/vendors/<name>/` with __init__.py defining
     a VendorParser subclass.
  2. Add a `from . import <name>` line below.
  3. Drop fixtures: `fixtures/<scenario>.eml` + `<scenario>.expected.json`.
  4. CI's parameterized vendor test will pick up the new fixtures automatically.
"""

from __future__ import annotations

# Each import triggers __init_subclass__ in parsers.base, registering the parser.
from . import (  # noqa: F401
    air_france,
    american,
    amtrak,
    avis,
    blacklane,
    fairmont,
    national,
    sncf,
    uber,
    united,
)
