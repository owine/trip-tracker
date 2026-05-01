"""AwardDetails model + display helpers. Spec §4.6, §6.6."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class AwardDetails(BaseModel):
    program: str = Field(min_length=1, max_length=100)
    points_spent: int = Field(ge=1)
    cash_copay_minor: int = Field(ge=0)
    cash_copay_currency: str = Field(min_length=3, max_length=3)
    cash_equivalent_minor: int | None = Field(default=None, ge=0)
    cash_equivalent_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("cash_copay_currency", "cash_equivalent_currency")
    @classmethod
    def upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @model_validator(mode="after")
    def _equivalent_pair(self) -> AwardDetails:
        """If cash_equivalent_minor is set, cash_equivalent_currency must be too.
        Saved-by-points rollup needs both — None currency would crash the rate lookup."""
        if self.cash_equivalent_minor is not None and not self.cash_equivalent_currency:
            raise ValueError("cash_equivalent_currency required when cash_equivalent_minor is set")
        return self


def k_format(points: int) -> str:
    """75000 → '75k', 1500 → '1.5k', 100 → '100'."""
    if points < 1000:
        return str(points)
    val = points / 1000.0
    if val == int(val):
        return f"{int(val)}k"
    return f"{val:.1f}k"


_PROGRAM_SHORT = {
    "Chase Ultimate Rewards": "Chase UR",
    "Amex Membership Rewards": "Amex MR",
    "Capital One Venture": "C1 Venture",
    "Citi ThankYou": "Citi TY",
    "Bilt Rewards": "Bilt",
    "United MileagePlus": "United",
    "Delta SkyMiles": "Delta",
    "American AAdvantage": "AAdvantage",
    "Alaska Mileage Plan": "Alaska",
    "Marriott Bonvoy": "Marriott",
    "Hyatt World of Hyatt": "Hyatt",
    "Hilton Honors": "Hilton",
    "IHG One Rewards": "IHG",
}


def program_short(program: str) -> str:
    """Map known program names to short forms; unknown programs pass through."""
    return _PROGRAM_SHORT.get(program, program)
