# Uber parser pack — type='transfer'

Handles `@uber.com`, `@receipt.uber.com`. Last verified: 2026.

Per spec §2 (Phase 3 design): captures EVERY Uber receipt as a transfer
segment. Trip clustering's ±1d adjacency + city-name match groups them
under the right trip. Same-city short rides during a trip become "transfer"
segments inside that trip. No filtering for noise — Phase 4+ may collapse
same-day same-city rides as a UI presentation concern.
