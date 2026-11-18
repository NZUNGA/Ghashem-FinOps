"""
Cost anomaly detector.

Compares this week's Azure spend against a trailing 8-week baseline and
raises a single, client-wide Finding when spend is significantly above
normal — the kind of "something changed" signal a human reviewer would
otherwise only catch by eyeballing the bill days after the fact.

Unlike the other analyzers, this one isn't keyed to a single Azure
resource: the finding is upserted against a synthetic resource_id (one
open cost_anomaly finding per client at a time), and the evidence names
the top contributing resources in plain English instead of linking to
one ARM resource ID.

"Today" is derived from the most recent DailyCost row on file, not the
wall clock — collector_cost's actual collection window is driven by
client_cfg.analysis_start/analysis_end, which is not guaranteed to reach
the real current date, so week boundaries are computed relative to
whatever data was actually collected.
"""

import logging
from datetime import timedelta
from statistics import median
from django.db.models import Max
from core.models import Client, PipelineRun, DailyCost
from core.utils import format_capped_pct
from pipeline.finding_utils import upsert_finding

log = logging.getLogger(__name__)

BASELINE_WEEKS       = 8
ANOMALY_MULTIPLIER   = 1.5
CRITICAL_MULTIPLIER  = 3.0
LOOKBACK_DAYS        = 90

# Guard 1 — minimum history: at least this many of the baseline weeks
# must show non-trivial spend, or a mostly-zero/new-account history drags
# the median down to near-zero and produces meaningless percentage spikes.
MIN_WEEKS_WITH_DATA     = 4
MEANINGFUL_WEEK_SPEND   = 1.00

# Guard 2 — minimum baseline floor: below this, any real spend looks like
# a massive percentage spike even when the dollar amounts are trivial.
MIN_BASELINE_WEEKLY = 5.00

# Synthetic — this finding isn't tied to one Azure resource, so there's
# no real ARM resource_id to key it on. Fixed value keeps upsert_finding's
# (client, resource_id, finding_type) uniqueness constraint meaning "one
# open cost-anomaly finding per client at a time".
RESOURCE_ID = "cost-anomaly"


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _friendly_name(resource_id: str, service_name: str) -> str:
    """Plain-English label for a cost row — last path segment of the
    resource ID if there is one, otherwise the Azure service name. Never
    returns a full ARM path."""
    if resource_id:
        return resource_id.rstrip("/").split("/")[-1]
    return service_name or "Unknown resource"


def run(client: Client, run_obj: PipelineRun) -> int:
    latest = DailyCost.objects.filter(client=client).aggregate(m=Max("date"))["m"]
    if latest is None:
        return 0  # no cost data collected yet

    window_start = latest - timedelta(days=LOOKBACK_DAYS)
    rows = list(
        DailyCost.objects.filter(client=client, date__gte=window_start, date__lte=latest)
        .values("date", "resource_id", "service_name", "billed_cost")
    )
    if not rows:
        return 0

    current_week_start = _week_start(latest)
    days_elapsed = (latest - current_week_start).days + 1

    weekly_totals = {}
    for r in rows:
        wk = _week_start(r["date"])
        weekly_totals[wk] = weekly_totals.get(wk, 0) + (r["billed_cost"] or 0)

    baseline_weeks = [
        wk for wk in weekly_totals
        if current_week_start - timedelta(weeks=BASELINE_WEEKS) <= wk < current_week_start
    ]
    if not baseline_weeks:
        return 0  # not enough history yet to call anything a spike

    # Guard 1 — minimum history
    weeks_with_data = sum(
        1 for wk in baseline_weeks if weekly_totals[wk] > MEANINGFUL_WEEK_SPEND
    )
    if weeks_with_data < MIN_WEEKS_WITH_DATA:
        log.warning(
            f"[{client.slug}] Anomaly detection skipped — insufficient cost "
            f"history ({weeks_with_data}/{BASELINE_WEEKS} weeks with meaningful data)"
        )
        return 0

    baseline = median(weekly_totals[wk] for wk in baseline_weeks)

    # Guard 2 — minimum baseline floor
    if baseline < MIN_BASELINE_WEEKLY:
        log.warning(
            f"[{client.slug}] Anomaly detection skipped — baseline too low "
            f"(${baseline:.2f}/week) to produce reliable anomaly signals"
        )
        return 0

    current_week_spend = weekly_totals.get(current_week_start, 0)
    current_week_annualized = current_week_spend * (7 / days_elapsed)

    if current_week_annualized > baseline * CRITICAL_MULTIPLIER:
        severity = "high"
    elif current_week_annualized > baseline * ANOMALY_MULTIPLIER:
        severity = "medium"
    else:
        return 0

    pct_over = ((current_week_annualized / baseline) - 1) * 100

    # Per-resource comparison: this week's cost vs. average weekly cost
    # over the baseline period, for resources active in the current week.
    baseline_week_count = len(baseline_weeks)
    per_resource_baseline = {}
    per_resource_current  = {}
    resource_labels       = {}

    for r in rows:
        key = r["resource_id"] or f"service:{r['service_name']}"
        resource_labels.setdefault(key, _friendly_name(r["resource_id"], r["service_name"]))
        wk = _week_start(r["date"])
        if wk == current_week_start:
            per_resource_current[key] = per_resource_current.get(key, 0) + (r["billed_cost"] or 0)
        elif wk in baseline_weeks:
            per_resource_baseline[key] = per_resource_baseline.get(key, 0) + (r["billed_cost"] or 0)

    spike_resources = []
    for key, this_week_cost in per_resource_current.items():
        avg_weekly = per_resource_baseline.get(key, 0) / baseline_week_count
        delta = this_week_cost - avg_weekly
        if delta <= 0:
            continue
        spike_resources.append({
            "name": resource_labels[key],
            "normal_weekly_cost": round(avg_weekly, 2),
            "this_week_cost": round(this_week_cost, 2),
            "delta": round(delta, 2),
            "pct_increase": round((delta / avg_weekly * 100) if avg_weekly > 0 else 100.0, 1),
        })

    spike_resources.sort(key=lambda r: r["delta"], reverse=True)
    top3 = spike_resources[:3]

    if top3:
        contributor_text = "; ".join(
            f"{r['name']} (${r['this_week_cost']:.2f}, up {r['pct_increase']:.0f}%)"
            for r in top3
        )
    else:
        contributor_text = (
            "no single resource stands out — the increase is spread across your spend"
        )

    weekly_delta = current_week_annualized - baseline
    monthly_delta = weekly_delta * (52 / 12)

    evidence = (
        f"This week's Azure spend is on pace for ${current_week_annualized:.2f}, "
        f"{format_capped_pct(pct_over)} above your normal weekly spend of ${baseline:.2f}. "
        f"Top contributors: {contributor_text}."
    )

    upsert_finding(
        client=client, run_obj=run_obj,
        resource_id=RESOURCE_ID, resource_name="Weekly Azure spend",
        resource_group="",
        finding_type="cost_anomaly", analyzer="cost_anomaly",
        severity=severity, monthly_savings=monthly_delta,
        evidence=evidence,
        action=(
            "Review the Spend page to identify which resources drove this "
            "week's increase, then deallocate or resize any not expected to "
            "run at this level."
        ),
        extra_data={
            "baseline_weekly_cost": round(baseline, 2),
            "current_week_annualized": round(current_week_annualized, 2),
            "pct_over_baseline": round(pct_over, 1),
            "days_elapsed_this_week": days_elapsed,
            "top_spike_resources": top3,
            "anomaly_level": "critical" if severity == "high" else "anomaly",
        },
    )
    return 1
