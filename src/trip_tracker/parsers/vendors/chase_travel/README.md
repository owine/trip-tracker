# Chase Travel parser pack — multi-segment booking portal

Handles `@chasetravel.com`, `chase-travel*@chase.com`, `noreply@chase.com`.
Last verified: 2026.

Chase Travel emails commonly contain flight + hotel + car bundles. This is
the only multi-segment vendor pack — it returns 1, 2, or 3 SegmentDrafts
from one email depending on which sections are present.

Most Chase Travel templates embed JSON-LD; the upstream JSON-LD strategy
handles those at higher confidence. This parser is the fallback for plain-
text emails or future template changes.
