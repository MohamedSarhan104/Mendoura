from decimal import Decimal, ROUND_DOWN

from django.core.exceptions import ValidationError

# ═══ Single source of truth. Percentages appear NOWHERE else. ═══
SPLIT_RULES = {
    'full': Decimal('70.00'),         # instructor did montage/production
    'script_only': Decimal('50.00'),  # platform did montage/production
}

# Subscription-driven revenue is a flat pool, unlike direct sales:
# production_type does not apply once a course is being paid for out of a
# subscriber's pool rather than a direct purchase.
SUBSCRIPTION_INSTRUCTOR_SHARE = Decimal('60.00')


def get_instructor_share(production_type: str) -> Decimal:
    try:
        return SPLIT_RULES[production_type]
    except KeyError:
        raise ValidationError(f"Unknown production_type: {production_type!r}")


def calculate_split(total: Decimal, share_pct: Decimal) -> tuple[Decimal, Decimal]:
    """Returns (instructor_amount, platform_amount).
    Guaranteed: instructor + platform == total, exactly. No lost cents."""
    instructor = (total * share_pct / Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_DOWN  # remainder -> platform
    )
    platform = total - instructor  # exact remainder
    return instructor, platform
