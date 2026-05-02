"""SNCF parser pack — type='train'."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers._email_text import extract_text
from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_TRAIN = re.compile(r"\bTrain\s+(\d+)", re.I)
_STATION_PAIR = re.compile(
    r"([A-Z][a-zA-Z\s]+)\s+(?:→|->|to)\s+([A-Z][a-zA-Z\s]+)\s*(?:\n|on)", re.I
)
_DATETIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class SncfParser(VendorParser):
    name: ClassVar[str] = "sncf"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        # SNCF apex + arbitrary subdomains + brand-suffixed labels:
        #   - `noreply@sncf.com` (apex)
        #   - `info@email.sncf.com` (subdomain)
        #   - `noreply@sncf-connect.com` (rebrand of OUI.sncf, suffix-branded)
        #   - `info@sncf-voyageurs.com` (corporate brand, suffix-branded)
        # Both `.com` and `.fr` because SNCF runs FR-only mail streams too.
        re.compile(r"@([\w.-]+\.)?sncf(-\w+)?\.(com|fr)$", re.I),
        # The `.sncf` TLD itself (real! ICANN-delegated brand TLD). Used by
        # `e-voyages.sncf` and other product brands. Matching the TLD as a
        # suffix catches any sub-label.
        re.compile(r"@[\w.-]+\.sncf$", re.I),
        # Legacy product brands (pre-rebrand).
        re.compile(r"@(tgv-europe|oui)\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = extract_text(msg)
        train = _TRAIN.search(body)
        stations = _STATION_PAIR.search(body)
        dt = _DATETIME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (stations and dt):
            return ParseResult(segments=[], confidence=0.0, source="rules:sncf")

        y, m, d, hh, mm = (int(g) for g in dt.groups())
        seg = SegmentDraft(
            type="train",
            confirmation_number=conf.group(1) if conf else None,
            provider="SNCF",
            start_at=datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC")),
            start_tz="UTC",
            start_location={"name": stations.group(1).strip()},
            end_location={"name": stations.group(2).strip()},
            details={"train_number": train.group(1) if train else None},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:sncf")
