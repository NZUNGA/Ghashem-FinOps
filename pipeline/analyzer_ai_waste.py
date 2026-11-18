"""
AI (Azure OpenAI) waste and governance analyzer.

Reads DailyAIUsage (populated by pipeline/collector_ai.py) and raises
three advisory finding types — idle deployments, token-usage spikes,
and missing cost-attribution tags. None of these carry a dollar
savings estimate the way the cost/zombie analyzers do (there's no
"delete this and save $X/mo" story for AI governance the way there is
for an orphaned disk), so monthly_savings is always 0 here.

FINDING 3 (untagged_ai_resource) is the one exception to this
codebase's "analyzers only read the DB" convention (see CLAUDE.md
Conventions) — tags aren't stored in any model, so it makes a live
Resource Graph call, following the same precedent
analyzer_reservations.py already set for the same reason.
"""

import logging
from datetime import date, timedelta
from statistics import median

from django.conf import settings
from core.models import Client, PipelineRun, DailyAIUsage
from core.auth import load_client_context
from pipeline.finding_utils import upsert_finding

log = logging.getLogger(__name__)

USAGE_WINDOW_DAYS = 30
BASELINE_WEEKS = 8
ANOMALY_MULTIPLIER = 1.5
CRITICAL_MULTIPLIER = 3.0
MIN_WEEKS_WITH_DATA = 4
MEANINGFUL_WEEK_TOKENS = 1


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _analyze_idle_deployments(client, run_obj, rows) -> int:
    """FINDING 1 — a deployment with zero total_tokens across every row
    in the last 30 days is flagged as idle. A deployment with no rows
    at all in that window never enters `rows` to begin with, so it's
    covered the same way — nothing to sum, nothing to flag as non-idle."""
    created = 0
    by_key = {}
    for r in rows:
        key = (r["resource_id"], r["deployment_name"])
        info = by_key.setdefault(key, {
            "resource_name": r["resource_name"],
            "resource_group": r["resource_group"],
            "total": 0,
        })
        info["total"] += r["total_tokens"] or 0

    for (resource_id, deployment_name), info in by_key.items():
        if info["total"] > 0:
            continue
        label = deployment_name or info["resource_name"]
        try:
            upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=resource_id,
                resource_name=info["resource_name"],
                resource_group=info["resource_group"],
                finding_type="idle_ai_deployment",
                analyzer="analyzer_ai_waste",
                severity="high",
                monthly_savings=0,
                evidence=(
                    f"Model deployment '{label}' on '{info['resource_name']}' "
                    f"has had zero token usage in the last 30 days. If this "
                    f"deployment uses Provisioned Throughput Units (PTUs), "
                    f"you are paying for reserved capacity with no return."
                ),
                action=(
                    "Delete this deployment if no longer needed. For "
                    "Standard pay-as-you-go deployments, no standing charge "
                    "applies — but removing unused deployments reduces "
                    "management overhead and eliminates accidental usage."
                ),
            )
            created += 1
        except Exception as exc:
            log.warning(f"[{client.slug}] idle_ai_deployment finding failed "
                        f"for {resource_id}: {exc}")
    return created


def _analyze_cost_spikes(client, run_obj, rows) -> int:
    """FINDING 2 — same median-baseline guard logic as
    pipeline/analyzer_anomaly.py, applied to weekly total_tokens per
    deployment instead of weekly Azure spend."""
    created = 0
    by_key = {}
    for r in rows:
        key = (r["resource_id"], r["deployment_name"])
        by_key.setdefault(key, []).append(r)

    for (resource_id, deployment_name), key_rows in by_key.items():
        resource_name = key_rows[0]["resource_name"]
        resource_group = key_rows[0]["resource_group"]

        latest = max(r["date"] for r in key_rows)
        current_week_start = _week_start(latest)

        weekly_totals = {}
        for r in key_rows:
            wk = _week_start(r["date"])
            weekly_totals[wk] = weekly_totals.get(wk, 0) + (r["total_tokens"] or 0)

        baseline_weeks = [
            wk for wk in weekly_totals
            if current_week_start - timedelta(weeks=BASELINE_WEEKS) <= wk < current_week_start
        ]
        if not baseline_weeks:
            continue  # not enough history yet to call anything a spike

        # Guard 1 — minimum history
        weeks_with_data = sum(
            1 for wk in baseline_weeks if weekly_totals[wk] > MEANINGFUL_WEEK_TOKENS
        )
        if weeks_with_data < MIN_WEEKS_WITH_DATA:
            continue

        baseline = median(weekly_totals[wk] for wk in baseline_weeks)

        # Guard 2 — zero/near-zero baseline makes any real usage look
        # like an infinite percentage spike
        if baseline <= 0:
            continue

        current_week_tokens = weekly_totals.get(current_week_start, 0)

        if current_week_tokens > baseline * CRITICAL_MULTIPLIER:
            severity = "high"
        elif current_week_tokens > baseline * ANOMALY_MULTIPLIER:
            severity = "medium"
        else:
            continue

        multiplier = current_week_tokens / baseline
        label = deployment_name or resource_name

        try:
            upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=resource_id,
                resource_name=resource_name,
                resource_group=resource_group,
                finding_type="ai_cost_spike",
                analyzer="analyzer_ai_waste",
                severity=severity,
                monthly_savings=0,
                evidence=(
                    f"Token usage on '{label}' spiked to "
                    f"{current_week_tokens:,} tokens this week — "
                    f"{multiplier:.1f}x the 8-week median of "
                    f"{int(baseline):,} tokens/week."
                ),
                action=(
                    "Investigate what triggered the usage spike. Common "
                    "causes: new feature launch, runaway automation loop, "
                    "or unauthorized usage. Review Azure Monitor logs for "
                    "caller identity and request patterns."
                ),
            )
            created += 1
        except Exception as exc:
            log.warning(f"[{client.slug}] ai_cost_spike finding failed "
                        f"for {resource_id}: {exc}")
    return created


def _analyze_untagged_resources(client, run_obj) -> int:
    """FINDING 3 — flags OpenAI resources missing CostCenter or Owner
    tags. Needs a live Resource Graph call, since tags aren't stored in
    any DB table — builds its own credential per subscription, the same
    self-contained pattern analyzer_reservations.py uses."""
    resource_rows = list(
        DailyAIUsage.objects.filter(client=client)
        .values("resource_id", "resource_name", "resource_group")
        .distinct()
    )
    if not resource_rows:
        return 0

    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest

    created = 0
    app_client_id = getattr(settings, "AZURE_APP_CLIENT_ID", "")
    vault_url = getattr(settings, "VAULT_URL", "")

    for sub in client.subscriptions.all():
        try:
            ctx = load_client_context(
                client_slug=client.slug,
                app_client_id=app_client_id,
                vault_url=vault_url,
                subscription_id=sub.subscription_id,
            )
            rg_client = ResourceGraphClient(credential=ctx.credential)

            id_list = ", ".join(f"'{r['resource_id'].lower()}'" for r in resource_rows)
            query = f"""
            Resources
            | where tolower(id) in ({id_list})
            | project id, tags
            """
            request = QueryRequest(subscriptions=[sub.subscription_id], query=query)
            result = rg_client.resources(request)
            tags_by_id = {row["id"].lower(): (row.get("tags") or {}) for row in result.data}
        except Exception as exc:
            log.warning(f"[{client.slug}] untagged_ai_resource tag lookup "
                        f"failed for subscription {sub.subscription_id}: {exc}")
            continue

        for r in resource_rows:
            rid_lower = r["resource_id"].lower()
            if rid_lower not in tags_by_id:
                continue  # not in this subscription
            tag_keys_lower = {k.lower() for k in tags_by_id[rid_lower].keys()}
            if "costcenter" in tag_keys_lower and "owner" in tag_keys_lower:
                continue
            try:
                upsert_finding(
                    client=client, run_obj=run_obj,
                    resource_id=r["resource_id"],
                    resource_name=r["resource_name"],
                    resource_group=r["resource_group"],
                    finding_type="untagged_ai_resource",
                    analyzer="analyzer_ai_waste",
                    severity="medium",
                    monthly_savings=0,
                    evidence=(
                        f"Azure OpenAI resource '{r['resource_name']}' has no "
                        f"CostCenter or Owner tag. Token costs cannot be "
                        f"attributed to a team or project."
                    ),
                    action=(
                        "Add CostCenter, Owner, and Environment tags to this "
                        "resource to enable cost attribution and showback "
                        "reporting."
                    ),
                )
                created += 1
            except Exception as exc:
                log.warning(f"[{client.slug}] untagged_ai_resource finding "
                            f"failed for {r['resource_id']}: {exc}")

    return created


def run(client_obj: Client, run_obj: PipelineRun) -> int:
    """Analyze DailyAIUsage for waste patterns. Returns count of findings created/updated."""
    window_start = date.today() - timedelta(days=USAGE_WINDOW_DAYS)
    rows = list(
        DailyAIUsage.objects.filter(client=client_obj, date__gte=window_start)
        .values("resource_id", "resource_name", "resource_group",
                 "deployment_name", "date", "total_tokens")
    )
    if not rows:
        return 0

    created = 0

    try:
        created += _analyze_idle_deployments(client_obj, run_obj, rows)
    except Exception as exc:
        log.warning(f"[{client_obj.slug}] idle_ai_deployment analysis failed: {exc}")

    try:
        created += _analyze_cost_spikes(client_obj, run_obj, rows)
    except Exception as exc:
        log.warning(f"[{client_obj.slug}] ai_cost_spike analysis failed: {exc}")

    try:
        created += _analyze_untagged_resources(client_obj, run_obj)
    except Exception as exc:
        log.warning(f"[{client_obj.slug}] untagged_ai_resource analysis failed: {exc}")

    return created
