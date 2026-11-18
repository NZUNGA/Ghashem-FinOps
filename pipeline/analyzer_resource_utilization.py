"""
General-purpose underutilization analyzer.

Most Azure resource types are billed for being provisioned, not for
being called — "idle" here means low utilization of the metric that
actually drives cost-effectiveness for that resource type: CPU for
VMs, transactions for Storage, requests for Web Apps, and (added
same-day as the Session 5b AI Governance expansion) API call volume
for Cognitive Services accounts of ANY kind — Vision, Speech,
Translation, Face, FormRecognizer, etc., not just OpenAI/AIServices.
Those don't produce token metrics at all, so pipeline/analyzer_token.py's
prompt/generated-ratio efficiency check never fires for them — call
volume (TotalCalls, already collected for every kind by
collector_metrics.py with no kind filter) is the right idle signal
for a per-transaction-billed service, the same way CPU is for a VM.

Each resource type gets its own utilization signal and threshold,
defined in RESOURCE_PROFILES below.
"""

from datetime import timedelta
from django.db.models import Sum, Avg
from core.models import Client, Finding, PipelineRun, DailyMetric, DailyCost
from pipeline.finding_utils import upsert_finding

IDLE_DAYS = 30

# Per-namespace utilization profile:
#   primary_metric   — the metric that best indicates "is this being used"
#   idle_threshold    — average value at/below which we call it idle
#   underutil_threshold — average value at/below which we call it underutilized
#                         (but not fully idle — still a finding, lower severity)
#   unit             — for evidence text only
#   resize_action / idle_action — human-readable recommendations
RESOURCE_PROFILES = {
    "Microsoft.Compute/virtualMachines": {
        "primary_metric": "Percentage CPU",
        "idle_threshold": 2.0,        # avg CPU% at/below this = idle
        "underutil_threshold": 10.0,  # avg CPU% at/below this = underutilized
        "unit": "% CPU",
        "idle_action": (
            "VM shows near-zero CPU usage. Deallocate (stop) if not needed, "
            "or delete if abandoned. Compute charges stop on deallocation; "
            "storage charges continue until the disk is deleted."
        ),
        "resize_action": (
            "VM is significantly over-provisioned for its actual load. "
            "Consider downsizing to a smaller VM size in the same family "
            "(e.g. D4s_v5 → D2s_v5) to cut compute cost roughly in half."
        ),
    },
    "Microsoft.Storage/storageAccounts": {
        "primary_metric": "Transactions",
        "idle_threshold": 1.0,        # avg transactions/day at/below this = idle
        "underutil_threshold": 50.0,
        "unit": "transactions/day",
        "idle_action": (
            "Storage account has almost no transaction activity. Confirm "
            "whether it's still referenced by any application before deleting. "
            "If it must be retained for compliance, consider moving to the "
            "Archive access tier to cut storage cost significantly."
        ),
        "resize_action": (
            "Low transaction volume relative to typical provisioned capacity. "
            "Review the access tier (Hot/Cool/Archive) — moving infrequently "
            "accessed data to Cool can reduce storage cost with minimal impact."
        ),
    },
    "Microsoft.Web/sites": {
        "primary_metric": "Requests",
        "idle_threshold": 5.0,        # avg requests/day at/below this = idle
        "underutil_threshold": 100.0,
        "unit": "requests/day",
        "idle_action": (
            "App Service shows almost no incoming traffic. Confirm the app "
            "is still in use before deleting. If it's a staging/dev slot kept "
            "around 'just in case', consider stopping it outside business "
            "hours or moving to a lower App Service Plan tier."
        ),
        "resize_action": (
            "Traffic volume is low relative to typical App Service Plan "
            "capacity. Consider downgrading the App Service Plan tier, or "
            "consolidating multiple low-traffic apps onto a single shared plan."
        ),
    },
    "Microsoft.CognitiveServices/accounts": {
        "primary_metric": "TotalCalls",
        "idle_threshold": 1.0,        # avg calls/day at/below this = idle
        "underutil_threshold": 50.0,
        "unit": "calls/day",
        "idle_action": (
            "Cognitive Services account shows almost no API call activity. "
            "Confirm it's still in use before deleting. Most Cognitive "
            "Services pricing tiers have no standing charge for zero usage "
            "— this finding only fires when real billed cost is already on "
            "record (e.g. a commitment-tier minimum, or reserved capacity), "
            "same has_cost_data guard as every other resource type here."
        ),
        "resize_action": (
            "Call volume is low relative to typical usage. If this account "
            "is on a commitment-tier pricing plan, review whether a lower "
            "tier or pay-as-you-go would better match actual usage."
        ),
    },
}


def run(client: Client, run_obj: PipelineRun) -> int:
    created = 0

    for namespace, profile in RESOURCE_PROFILES.items():
        metric_name = profile["primary_metric"]

        resource_ids = (
            DailyMetric.objects
            .filter(client=client, metric_namespace=namespace, metric_name=metric_name)
            .values_list("resource_id", flat=True)
            .distinct()
        )

        for rid in resource_ids:
            qs = (
                DailyMetric.objects
                .filter(client=client, resource_id=rid, metric_name=metric_name)
                .order_by("date")
            )
            if not qs.exists():
                continue

            rows = list(qs.values("date", "value"))
            values = [r["value"] or 0 for r in rows]
            avg_value = sum(values) / len(values) if values else 0

            last_date = rows[-1]["date"]
            cutoff = last_date - timedelta(days=IDLE_DAYS)
            last_30 = [r["value"] or 0 for r in rows if r["date"] >= cutoff]
            avg_last_30 = sum(last_30) / len(last_30) if last_30 else 0

            resource_name  = qs.first().resource_name or rid
            resource_group = qs.first().resource_group or ""

            has_cost_data = DailyCost.objects.filter(
                client=client, resource_id__iexact=rid
            ).exists()
            if not has_cost_data:
                # No cost data matched this resource — skip rather than
                # report a $0 finding that isn't actionable
                continue

            window_days = max((client.analysis_end - client.analysis_start).days, 1)
            monthly_cost = (
                DailyCost.objects.filter(client=client, resource_id__iexact=rid)
                .aggregate(total=Sum("billed_cost"))["total"] or 0
            ) * (30 / window_days)

            # ── Fully idle ──
            if avg_last_30 <= profile["idle_threshold"]:
                upsert_finding(
                    client=client, run_obj=run_obj,
                    resource_id=rid, resource_name=resource_name,
                    resource_group=resource_group,
                    finding_type="idle_account", analyzer="resource_utilization",
                    severity="high", monthly_savings=monthly_cost,
                    evidence=(
                        f"'{resource_name}' ({namespace.split('/')[-1]}) averaged "
                        f"{avg_last_30:.1f} {profile['unit']} over the last 30 days "
                        f"— effectively idle. Billing ~${monthly_cost:.2f}/month."
                    ),
                    action=profile["idle_action"],
                    extra_data={
                        "resource_type": namespace, "idle_type": "fully_idle",
                        "primary_metric": metric_name,
                        "avg_value_last_30": round(avg_last_30, 2),
                    },
                )
                created += 1
                continue

            # ── Underutilized (not idle, but low) ──
            if avg_last_30 <= profile["underutil_threshold"]:
                savings = monthly_cost * 0.35
                upsert_finding(
                    client=client, run_obj=run_obj,
                    resource_id=rid, resource_name=resource_name,
                    resource_group=resource_group,
                    finding_type="wrong_tier", analyzer="resource_utilization",
                    severity="medium", monthly_savings=savings,
                    evidence=(
                        f"'{resource_name}' ({namespace.split('/')[-1]}) averaged "
                        f"{avg_last_30:.1f} {profile['unit']} over the last 30 days "
                        f"— underutilized relative to typical provisioned capacity. "
                        f"Current spend ~${monthly_cost:.2f}/month."
                    ),
                    action=profile["resize_action"],
                    extra_data={
                        "resource_type": namespace, "idle_type": "underutilized",
                        "primary_metric": metric_name,
                        "avg_value_last_30": round(avg_last_30, 2),
                    },
                )
                created += 1

    return created
