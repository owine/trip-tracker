# American Airlines parser pack

Handles `noreply@aa.com`, `notify@aa.com`, `*@email.aa.com`. Last verified: 2026.

Falls back to Haiku when the AA template includes JSON-LD (most cases) — JSON-LD
strategy runs first and at higher confidence.
