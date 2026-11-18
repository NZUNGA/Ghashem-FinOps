from decimal import Decimal
from django.db.models import Sum


def calculate_monthly_fee(client):
    """
    Returns the monthly Ghashem fee for a client as a Decimal.
    Uses tier_override_monthly if set (fixed-price deals).
    Otherwise calculates from last 30 days of Azure spend.

    Tiers:
      visibility: 2% of spend, min $299, max $599 CAD
      advisory:   3% of spend, min $599, no cap
      managed:    4% of spend, min $1,200, no cap
    """
    if client.tier_override_monthly is not None:
        return client.tier_override_monthly

    from datetime import date, timedelta
    from core.models import DailyCost

    cutoff = date.today() - timedelta(days=30)
    monthly_spend = (
        DailyCost.objects
        .filter(client=client, date__gte=cutoff)
        .aggregate(s=Sum("billed_cost"))["s"] or 0
    )

    rates = {
        "visibility": Decimal("0.02"),
        "advisory":   Decimal("0.03"),
        "managed":    Decimal("0.04"),
    }
    floors = {
        "visibility": Decimal("299"),
        "advisory":   Decimal("599"),
        "managed":    Decimal("1200"),
    }
    caps = {
        "visibility": Decimal("599"),
        "advisory":   Decimal("999"),
        "managed":    None,
    }

    rate  = rates[client.tier]
    floor = floors[client.tier]
    cap   = caps[client.tier]

    fee = Decimal(str(monthly_spend)) * rate
    fee = max(fee, floor)
    if cap is not None:
        fee = min(fee, cap)

    return fee.quantize(Decimal("0.01"))
