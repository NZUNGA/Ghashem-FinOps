"""
Zombie resource analyzer.

Converts the raw Resource Graph results from collector_zombie.py into
Finding rows. Unlike the metrics-based analyzers, savings here are
mostly *known list prices* rather than derived from actual billing
data — an unattached Public IP costs a fixed ~$3-4/month regardless
of usage, so we don't need DailyCost to estimate the waste.

Where real cost data IS available (matched by resource_id against
DailyCost), we prefer it — it's more accurate than the flat estimate.
"""

from django.db.models import Sum
from core.models import Client, Finding, PipelineRun, DailyCost
from pipeline.finding_utils import upsert_finding as _upsert_finding_shared

# Conservative list-price estimates (USD/month) for resources that
# bill a flat rate regardless of usage. Used as a fallback when no
# matching DailyCost row exists (common for very recently created
# resources that haven't appeared in a cost export yet).
FLAT_RATE_ESTIMATES = {
    "unattached_public_ip": 3.65,     # Standard SKU static IP, list price
    "unattached_nic":       0.0,      # NICs themselves are free; flagged for hygiene only
    "unused_nsg":           0.0,      # NSGs are free; flagged for hygiene only
}


def _matched_cost(client: Client, resource_id: str) -> float:
    """Looks up actual 6-month cost for a resource, case-insensitive."""
    total = (
        DailyCost.objects.filter(client=client, resource_id__iexact=resource_id)
        .aggregate(s=Sum("billed_cost"))["s"]
    )
    window_days = max((client.analysis_end - client.analysis_start).days, 1)
    return (total or 0) * (30 / window_days)


def _upsert_finding(client, run_obj, finding_type, severity, resource,
                    monthly_savings, evidence, action):
    """Thin wrapper adapting the resource-dict convention to the shared helper."""
    _upsert_finding_shared(
        client=client, run_obj=run_obj,
        resource_id=resource["id"],
        resource_name=resource.get("name", ""),
        resource_group=resource.get("resourceGroup", ""),
        finding_type=finding_type,
        analyzer="zombie_resources",
        severity=severity,
        monthly_savings=monthly_savings,
        evidence=evidence,
        action=action,
        extra_data={k: v for k, v in resource.items()
                    if k not in ("id", "name", "resourceGroup")},
    )


def analyze_unattached_public_ips(client, run_obj, items: list) -> int:
    for r in items:
        cost = _matched_cost(client, r["id"]) or FLAT_RATE_ESTIMATES["unattached_public_ip"]
        _upsert_finding(
            client, run_obj, "unattached_public_ip", "medium", r, cost,
            evidence=(
                f"Public IP '{r['name']}' ({r.get('sku', 'unknown')} SKU) is reserved but "
                f"not attached to any resource. Billing ~${cost:.2f}/month for an address "
                f"that routes nowhere."
            ),
            action="Release this Public IP if it's no longer needed. If reserved for "
                   "planned future use, document why and set a review date.",
        )
    return len(items)


def analyze_orphaned_disks(client, run_obj, items: list) -> int:
    for r in items:
        size_gb = r.get("diskSizeGb") or 0
        sku = r.get("sku", "")
        # Rough list-price estimate by SKU family when no cost match exists
        per_gb_rate = 0.10 if "Premium" in sku else 0.05 if "Standard" in sku else 0.04
        fallback_cost = size_gb * per_gb_rate
        cost = _matched_cost(client, r["id"]) or fallback_cost

        severity = "high" if cost > 20 else "medium"
        _upsert_finding(
            client, run_obj, "orphaned_disk", severity, r, cost,
            evidence=(
                f"Managed disk '{r['name']}' ({size_gb} GB, {sku}) is not attached to any "
                f"VM — likely left behind after a VM was deleted. Billing ~${cost:.2f}/month "
                f"for storage nothing is using."
            ),
            action="Confirm the disk's data isn't needed, then delete it. If it might "
                   "contain recoverable data, create a snapshot first, then delete the disk.",
        )
    return len(items)


def analyze_unattached_nics(client, run_obj, items: list) -> int:
    for r in items:
        _upsert_finding(
            client, run_obj, "unattached_nic", "low", r, 0.0,
            evidence=(
                f"Network interface '{r['name']}' is not attached to any VM. NICs "
                f"themselves don't bill directly, but this is resource-group clutter "
                f"that complicates cleanup and audits."
            ),
            action="Delete this orphaned network interface as part of routine hygiene.",
        )
    return len(items)


def analyze_unused_nsgs(client, run_obj, items: list) -> int:
    for r in items:
        _upsert_finding(
            client, run_obj, "unused_nsg", "low", r, 0.0,
            evidence=(
                f"Network security group '{r['name']}' has no associated subnets or "
                f"network interfaces. Doesn't bill directly, but unused NSGs make "
                f"security audits harder — reviewers waste time on rules that protect nothing."
            ),
            action="Delete this NSG, or document why it's retained (e.g. template for future use).",
        )
    return len(items)


def analyze_aging_snapshots(client, run_obj, items: list) -> int:
    for r in items:
        size_gb = r.get("diskSizeGb") or 0
        fallback_cost = size_gb * 0.05  # snapshot storage, rough list price
        cost = _matched_cost(client, r["id"]) or fallback_cost

        _upsert_finding(
            client, run_obj, "aging_snapshot", "medium", r, cost,
            evidence=(
                f"Snapshot '{r['name']}' ({size_gb} GB) was created on "
                f"{r.get('timeCreated', 'an unknown date')} and is older than 90 days "
                f"with no retention policy. Billing ~${cost:.2f}/month."
            ),
            action="Review whether this snapshot is still needed for backup/compliance. "
                   "If not, delete it. If retention is required, move to a lifecycle "
                   "policy instead of manual snapshots.",
        )
    return len(items)


def analyze_stopped_not_deallocated(client, run_obj, items: list) -> int:
    for r in items:
        cost = _matched_cost(client, r["id"])
        if cost == 0:
            continue  # no billing data yet, skip rather than guess at compute cost

        _upsert_finding(
            client, run_obj, "stopped_not_deallocated", "high", r, cost,
            evidence=(
                f"VM '{r['name']}' ({r.get('vmSize', 'unknown size')}) is stopped but "
                f"NOT deallocated — Azure is still billing full compute charges "
                f"(~${cost:.2f}/month) even though the OS is shut down. Only "
                f"'Deallocate' (not OS shutdown) actually stops compute billing."
            ),
            action="Deallocate this VM via the Azure Portal or CLI "
                   "(az vm deallocate), not just an OS-level shutdown, to stop "
                   "compute charges immediately.",
        )
    return len(items)


def analyze_empty_load_balancers(client, run_obj, items: list) -> int:
    for r in items:
        sku = r.get("sku", "").lower()
        lb_fallback = 0.0 if sku == "basic" else 18.0  # Basic LB is free; Standard ~$18/mo
        cost = _matched_cost(client, r["id"]) or lb_fallback
        _upsert_finding(
            client, run_obj, "empty_load_balancer", "medium", r, cost,
            evidence=(
                f"Load balancer '{r['name']}' ({r.get('sku', 'unknown')} SKU) has no "
                f"backend pool members — it's provisioned and billing ~${cost:.2f}/month "
                f"but routing no traffic anywhere."
            ),
            action="Delete this load balancer if the backend service was decommissioned, "
                   "or attach it to its intended backend pool if setup was left incomplete.",
        )
    return len(items)


def analyze_no_autoshutdown(client, run_obj, items: list) -> int:
    for r in items:
        cost = _matched_cost(client, r["id"])
        if cost == 0:
            continue

        # Rough estimate: running 24/7 vs a typical 12hr/weekday dev schedule
        # is about 3.5x more hours — flag the avoidable portion only
        potential_savings = cost * 0.65

        _upsert_finding(
            client, run_obj, "no_autoshutdown", "low", r, potential_savings,
            evidence=(
                f"VM '{r['name']}' has no environment tag indicating production use "
                f"and no auto-shutdown schedule configured. Running 24/7 at "
                f"~${cost:.2f}/month vs an estimated ~${cost - potential_savings:.2f}/month "
                f"on a typical 12hr/weekday dev schedule."
            ),
            action="Add an auto-shutdown schedule (Azure DevTest Labs feature, free) "
                   "for non-production hours, or confirm this VM genuinely needs to "
                   "run continuously and tag it 'environment: production' to exclude "
                   "it from future scans.",
        )
    return len(items)


def run(client: Client, run_obj: PipelineRun, zombie_data: dict) -> int:
    """
    Runs all zombie analyzers against the collected Resource Graph data.
    Returns total findings created.
    """
    total = 0
    total += analyze_unattached_public_ips(client, run_obj, zombie_data["unattached_public_ip"])
    total += analyze_orphaned_disks(client, run_obj, zombie_data["orphaned_disk"])
    total += analyze_unattached_nics(client, run_obj, zombie_data["unattached_nic"])
    total += analyze_unused_nsgs(client, run_obj, zombie_data["unused_nsg"])
    total += analyze_aging_snapshots(client, run_obj, zombie_data["aging_snapshot"])
    total += analyze_stopped_not_deallocated(client, run_obj, zombie_data["stopped_not_deallocated"])
    total += analyze_empty_load_balancers(client, run_obj, zombie_data["empty_load_balancer"])
    total += analyze_no_autoshutdown(client, run_obj, zombie_data["no_autoshutdown"])
    return total
