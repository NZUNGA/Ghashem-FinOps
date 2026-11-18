from datetime import timedelta
from django.db.models import Sum
from core.models import Client, Finding, PipelineRun, DailyMetric, DailyCost
from pipeline.finding_utils import upsert_finding

IDLE_DAYS       = 30
NEAR_IDLE_MAX   = 10   # avg calls/day
S0_FLOOR_COST   = 10.0


def run(client: Client, run_obj: PipelineRun) -> int:
    cog_ns = "Microsoft.CognitiveServices/accounts"

    resource_ids = (
        DailyMetric.objects
        .filter(client=client, metric_namespace=cog_ns, metric_name="TotalCalls")
        .values_list("resource_id", flat=True)
        .distinct()
    )

    created = 0

    for rid in resource_ids:
        qs = (
            DailyMetric.objects
            .filter(client=client, resource_id=rid, metric_name="TotalCalls")
            .order_by("date")
        )
        if not qs.exists():
            continue

        rows        = list(qs.values("date", "value"))
        total_calls = sum(r["value"] or 0 for r in rows)
        last_date   = rows[-1]["date"]
        cutoff      = last_date - timedelta(days=IDLE_DAYS)

        # Consecutive idle days from the end
        consec_idle = 0
        for r in reversed(rows):
            if (r["value"] or 0) > 0:
                break
            consec_idle += 1

        last_30 = [r["value"] or 0 for r in rows if r["date"] >= cutoff]
        avg_last_30 = sum(last_30) / len(last_30) if last_30 else 0

        last_active = next(
            (str(r["date"]) for r in reversed(rows) if (r["value"] or 0) > 0),
            "never"
        )

        resource_name  = qs.first().resource_name or rid
        resource_group = qs.first().resource_group or ""

        # Monthly cost
        window_days = max((client.analysis_end - client.analysis_start).days, 1)
        cost_qs = DailyCost.objects.filter(
            client=client, resource_id__iexact=rid,
        ).aggregate(total=Sum("billed_cost"))
        if cost_qs["total"]:
            monthly_cost = cost_qs["total"] * (30 / window_days)
        else:
            # S0_FLOOR_COST is already a monthly figure — don't prorate it.
            monthly_cost = S0_FLOOR_COST

        # Fully idle (zero calls ever)
        if total_calls == 0:
            _, c = upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=rid, resource_name=resource_name,
                resource_group=resource_group,
                finding_type="idle_account", analyzer="cognitive_services",
                severity="high", monthly_savings=monthly_cost,
                evidence=(
                    f"'{resource_name}' received zero calls across the entire "
                    f"{len(rows)}-day window. Billing ~${monthly_cost:.2f}/month "
                    f"with no utilization."
                ),
                action=(
                    "Delete if from a discontinued project. If may be needed again, "
                    "disable it and set a 90-day review reminder."
                ),
                extra_data={"idle_type": "fully_idle", "last_call_date": "never",
                            "consecutive_idle_days": len(rows)},
            )
            created += int(c)
            continue

        # Idle at end
        if consec_idle >= IDLE_DAYS:
            _, c = upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=rid, resource_name=resource_name,
                resource_group=resource_group,
                finding_type="idle_account", analyzer="cognitive_services",
                severity="high", monthly_savings=monthly_cost,
                evidence=(
                    f"'{resource_name}' last had calls on {last_active} and "
                    f"has been idle for {consec_idle} consecutive days. "
                    f"Still billing ~${monthly_cost:.2f}/month."
                ),
                action=(
                    f"Confirm with the owning team. Last active: {last_active}. "
                    f"Delete or disable if abandoned."
                ),
                extra_data={"idle_type": "fully_idle", "last_call_date": last_active,
                            "consecutive_idle_days": consec_idle},
            )
            created += int(c)
            continue

        # Near idle
        if 0 < avg_last_30 <= NEAR_IDLE_MAX:
            savings = monthly_cost * 0.60
            _, c = upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=rid, resource_name=resource_name,
                resource_group=resource_group,
                finding_type="idle_account", analyzer="cognitive_services",
                severity="medium", monthly_savings=savings,
                evidence=(
                    f"'{resource_name}' averaged only {avg_last_30:.1f} calls/day "
                    f"over the last 30 days — likely over-provisioned at "
                    f"~${monthly_cost:.2f}/month."
                ),
                action=(
                    "Consider consolidating into an existing higher-utilization account "
                    "of the same kind, or downgrading to a lower commitment tier."
                ),
                extra_data={"idle_type": "near_idle", "last_call_date": last_active,
                            "avg_daily_calls_last_30": round(avg_last_30, 2)},
            )
            created += int(c)

    return created
