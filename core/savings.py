"""
Canonical savings helper — single source of truth for
"how much money could this client save monthly?"

The same exclusion logic `summary()` uses is now shared by every surface
that shows a savings number so they all agree:

  1. Exclude `cost_anomaly` findings — their estimated_monthly_savings_usd
     is a projected spike (extra spend already happening), not something
     the client can "save" by taking an action.
  2. Exclude findings on resources live_check.py has confirmed deleted —
     those resources are already gone and can't be acted on.
  3. Cap each resource's combined savings at its real monthly cost —
     multiple findings on one resource can sum above 100% of its cost,
     which is impossible to actually realize.

Callers that previously did a raw Sum(estimated_monthly_savings_usd) on
open findings — findings(), client_list(), compute_brief_stats(),
spend() — should call canonical_monthly_savings() instead.

The function intentionally mirrors the logic in dashboard/views.py::
summary() / _capped_total_savings() / _monthly_cost_by_resource()
rather than calling those directly, because core/ must not import from
dashboard/ (import-direction rule: dashboard imports core, not vice
versa). The logic is small enough to duplicate without risk of drift —
any change to the exclusion/cap rules should be applied here too.
"""

from datetime import date, timedelta

from django.db.models import Sum, Min, Max
from django.db.models.functions import Lower

from core.models import Client, Finding, ResourceStatus, DailyCost


# Finding types that carry $0 savings by design and must never inflate
# the savings total even if they accidentally get a non-zero estimate
# (mirrors AI_GOVERNANCE_FINDING_TYPES in dashboard/views.py).
_EXCLUDED_FINDING_TYPES = {
    "cost_anomaly",
    "ai_local_auth_enabled",
    "ai_public_network_access",
    "ai_no_content_filter",
    "ai_no_private_endpoint",
    "shadow_ai_resource",
    "idle_ai_deployment",
    "ai_cost_spike",
    "untagged_ai_resource",
}

MIN_BILLING_SPAN_DAYS = 7


def _deleted_resource_ids(client: Client) -> set:
    """Resource ids live_check.py confirmed deleted, normalized."""
    return {
        rid.lower().strip().rstrip("/")
        for rid in ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }


def _monthly_cost_by_resource(client: Client, window_start: date) -> dict:
    """
    {resource_id.lower(): monthly_cost} using each resource's actual
    billed span, mirroring dashboard/views.py::_monthly_cost_by_resource().
    Deleted resources are excluded so the cap reflects real current cost.
    """
    deleted = _deleted_resource_ids(client)
    qs = DailyCost.objects.filter(client=client, date__gte=window_start).exclude(resource_id="")
    if deleted:
        qs = qs.annotate(rid_lc=Lower("resource_id")).exclude(rid_lc__in=deleted)

    monthly = {}
    for r in (
        qs.annotate(rid=Lower("resource_id"))
        .values("rid")
        .annotate(total=Sum("billed_cost"), first=Min("date"), last=Max("date"))
    ):
        span = max((r["last"] - r["first"]).days + 1, MIN_BILLING_SPAN_DAYS)
        monthly[r["rid"]] = (r["total"] or 0) * 30.0 / span
    return monthly


def _cap_savings(raw_savings: float, monthly_cost: float) -> float:
    """Mirrors dashboard/views.py::_cap_savings()."""
    if monthly_cost > 0:
        return min(raw_savings, monthly_cost)
    return raw_savings


def canonical_monthly_savings(client: Client) -> float:
    """
    Returns the canonical monthly savings figure for a client — the same
    number the Overview page shows. Use this everywhere a savings total
    appears: Findings page topbar, staff client list, PDF brief, spend page.

    Returns 0.0 if the client has no open findings or no cost data.
    """
    window_start = client.analysis_start or (date.today() - timedelta(days=90))
    deleted_ids = _deleted_resource_ids(client)

    savings_qs = (
        Finding.objects
        .filter(client=client, status="open")
        .exclude(resource_id__in=deleted_ids)
        .exclude(finding_type__in=_EXCLUDED_FINDING_TYPES)
    )

    monthly_cost_by_resource = _monthly_cost_by_resource(client, window_start)

    total = 0.0
    for row in (
        savings_qs.values("resource_id")
        .annotate(s=Sum("estimated_monthly_savings_usd"))
    ):
        rid = row["resource_id"].lower().strip().rstrip("/")
        monthly_cost = monthly_cost_by_resource.get(rid, 0) or 0
        total += _cap_savings(row["s"] or 0.0, monthly_cost)
    return round(total, 2)
