"""Money conversion helpers.

Amounts are persisted as integer minor units (paise). SQLite has no exact
decimal type - `Numeric` round-trips through a C double, which both loses cents
and makes `ORDER BY amount` unreliable. Integers avoid both problems, and the
Decimal boundary lives here.
"""

from decimal import ROUND_HALF_UP, Decimal

MINOR_UNIT_SCALE = 100
TWO_PLACES = Decimal("0.01")


def to_minor_units(amount: Decimal) -> int:
    """Decimal rupees -> integer paise. Input is already validated to 2 dp."""
    return int(amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP) * MINOR_UNIT_SCALE)


def from_minor_units(amount_minor: int) -> Decimal:
    """Integer paise -> Decimal rupees, always exactly 2 dp."""
    return (Decimal(amount_minor) / MINOR_UNIT_SCALE).quantize(TWO_PLACES)
