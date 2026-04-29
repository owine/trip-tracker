"""VendorParser contract + ParseResult/SegmentDraft Pydantic schemas + registry.

Subclassing VendorParser auto-registers via __init_subclass__.
parsers/vendors/__init__.py imports each subpackage to trigger registration.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from email.message import EmailMessage
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

SegmentType = Literal["flight", "lodging", "car", "train", "transfer", "activity"]


class SegmentDraft(BaseModel):
    """Pydantic mirror of the Segment ORM shape, no DB columns."""

    type: SegmentType
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    confirmation_number: str | None = None
    provider: str | None = None
    start_at: datetime
    start_tz: str
    end_at: datetime | None = None
    end_tz: str | None = None
    start_location: dict[str, Any] | None = None
    end_location: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ParseResult(BaseModel):
    """Output of any parser strategy."""

    segments: list[SegmentDraft] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # "json-ld" | "rules:<name>" | "llm:haiku-4-5"
    warnings: list[str] = Field(default_factory=list)


_REGISTRY: list[type[VendorParser]] = []


class VendorParser(ABC):
    """Each vendor pack subclasses this. Auto-registered on subclass creation.

    sender_patterns: list of compiled regexes. The dispatcher matches the
    From: header against each parser's patterns; sorts by most-specific
    pattern first (longest pattern wins).
    """

    name: ClassVar[str]
    sender_patterns: ClassVar[list[re.Pattern[str]]]
    confidence_floor: ClassVar[float] = 0.85

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "name"):
            raise TypeError(f"{cls.__name__} must define `name` ClassVar")
        if not hasattr(cls, "sender_patterns"):
            raise TypeError(f"{cls.__name__} must define `sender_patterns` ClassVar")
        _REGISTRY.append(cls)

    @abstractmethod
    def parse(self, msg: EmailMessage) -> ParseResult: ...

    @classmethod
    def matches(cls, from_address: str) -> bool:
        return any(p.search(from_address) for p in cls.sender_patterns)


def get_registry() -> list[type[VendorParser]]:
    """All registered vendor parser classes (no ordering guarantee)."""
    return list(_REGISTRY)


def select_parsers(from_address: str) -> list[type[VendorParser]]:
    """Return the parsers whose sender_patterns match `from_address`,
    sorted by most-specific pattern first.

    'Most specific' = longest pattern.string. Ties broken by name.
    """
    matched = [p for p in _REGISTRY if p.matches(from_address)]

    def specificity(parser: type[VendorParser]) -> tuple[int, str]:
        longest = max(len(pat.pattern) for pat in parser.sender_patterns)
        return (-longest, parser.name)

    return sorted(matched, key=specificity)
