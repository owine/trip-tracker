# Air France parser pack

Handles email confirmations from `noreply@airfrance.fr`, `noreply@airfrance.com`,
`flyingblue@airfrance.com`. Last verified format: 2026.

The current Air France template embeds a JSON-LD `FlightReservation` block
which the upstream JSON-LD strategy will already pick up at confidence 0.95.
This parser is the **fallback** for the (rarer) plain-HTML version that arrives
when the user is on the older AF template (no JSON-LD).
