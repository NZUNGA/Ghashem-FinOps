"""
Zombie / phantom resource collector.

Unlike the metrics-based collectors, this one needs no Monitor API
calls at all — every signal it needs (attachment state, power state,
backend pool membership) is already present in Resource Graph's
resource properties. This makes it fast, free of the 90-day Monitor
retention limit, and immune to the 429 throttling we hit on Cost
Management — Resource Graph has much higher rate limits.

One Resource Graph query per zombie category, each returning just
the fields needed to make a yes/no waste determination.
"""

import logging
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest

log = logging.getLogger(__name__)


def _run_query(rg_client: ResourceGraphClient, subscription_ids: list,
               query: str, category: str = "") -> list:
    """
    Runs a single Resource Graph query and returns the results as a list.
    Returns an empty list (rather than raising) on failure so one bad
    query never kills the entire zombie scan — the other 7 categories
    still run and produce findings.
    """
    try:
        request = QueryRequest(subscriptions=subscription_ids, query=query)
        result = rg_client.resources(request)
        return list(result.data)
    except Exception as exc:
        log.warning(f"  Zombie query failed [{category}]: {exc}")
        return []


# ── Shared zombie-state conditions ──────────────────────────────────────
# (resource_type, condition_clause) per category — the condition_clause is
# a self-contained pipe fragment (starting with `|`) that decides whether
# a resource of that type is CURRENTLY in the zombie state. Only the
# categories live_check.py needs to re-verify on demand are extracted here;
# the full collect_* queries below build from these so both the full scan
# and live_check's targeted single-resource re-check apply the identical
# waste definition. Categories not re-checked by live_check (unattached_nic,
# unused_nsg, aging_snapshot, no_autoshutdown) keep their query inline.
ZOMBIE_CONDITIONS = {
    "unattached_public_ip": (
        "microsoft.network/publicipaddresses",
        """
        | extend ipConfigId = tostring(properties.ipConfiguration.id)
        | extend nicId = tolower(extract(@'(.+)/ipConfigurations/', 1, ipConfigId))
        | join kind=leftouter (
            resources
            | where type =~ 'microsoft.network/networkinterfaces'
            | project nicId = tolower(id), nicVm = properties.virtualMachine
        ) on nicId
        | where isnull(properties.ipConfiguration) or isnull(nicVm)
        """,
    ),
    "orphaned_disk": (
        "microsoft.compute/disks",
        # managedBy can be null OR an empty string on unattached disks —
        # Azure is inconsistent. Handle both cases explicitly.
        "| where isnull(managedBy) or managedBy == ''",
    ),
    "empty_load_balancer": (
        "microsoft.network/loadbalancers",
        # array_length(null) returns null, not 0, so a backend pool whose
        # backendIPConfigurations is missing entirely (rather than an
        # empty array) needs an explicit isnull() check or it silently
        # falls through the array_length == 0 comparison.
        """
        | extend firstPoolBackendConfigs = properties.backendAddressPools[0].properties.backendIPConfigurations
        | where array_length(properties.backendAddressPools) == 0
            or isnull(firstPoolBackendConfigs)
            or array_length(firstPoolBackendConfigs) == 0
        """,
    ),
    "stopped_not_deallocated": (
        "microsoft.compute/virtualmachines",
        """
        | extend powerState = tostring(properties.extended.instanceView.powerState.code)
        | where powerState == 'PowerState/stopped'
        """,
    ),
}


def recheck_query(category: str, resource_ids: list) -> str:
    """
    Builds a KQL query returning the subset of resource_ids STILL in the
    zombie state for `category`, using the exact same condition as the
    corresponding collect_* function — just scoped to specific resource
    IDs instead of scanning the whole subscription. Pure query-building,
    no Azure call — the caller (live_check.py) executes it and owns
    error handling, since a failed re-check must never be silently
    treated as "resolved".
    """
    resource_type, condition = ZOMBIE_CONDITIONS[category]
    id_list = ", ".join(f"'{rid.lower()}'" for rid in resource_ids)
    return f"""
    resources
    | where type =~ '{resource_type}'
    | where tolower(id) in ({id_list})
    {condition}
    | project id
    """


def collect_unattached_public_ips(rg_client, subscription_ids: list) -> list:
    # An IP is orphaned either because it has no ipConfiguration at all,
    # or because its ipConfiguration points to a NIC that is itself
    # detached from any VM — a dangling NIC still holds the IP but
    # nothing is actually using it.
    resource_type, condition = ZOMBIE_CONDITIONS["unattached_public_ip"]
    query = f"""
    resources
    | where type =~ '{resource_type}'
    {condition}
    | project id, name, resourceGroup, location,
              sku = tostring(sku.name),
              allocationMethod = tostring(properties.publicIPAllocationMethod)
    """
    return _run_query(rg_client, subscription_ids, query, "unattached_public_ip")


def collect_orphaned_disks(rg_client, subscription_ids: list) -> list:
    # managedBy can be null OR an empty string on unattached disks —
    # Azure is inconsistent. Handle both cases explicitly.
    resource_type, condition = ZOMBIE_CONDITIONS["orphaned_disk"]
    query = f"""
    resources
    | where type =~ '{resource_type}'
    {condition}
    | project id, name, resourceGroup, location,
              diskSizeGb = toint(properties.diskSizeGb),
              sku = tostring(sku.name),
              timeCreated = tostring(properties.timeCreated)
    """
    return _run_query(rg_client, subscription_ids, query, "orphaned_disk")


def collect_unattached_nics(rg_client, subscription_ids: list) -> list:
    query = """
    resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | where isnull(properties.virtualMachine)
    | project id, name, resourceGroup, location
    """
    return _run_query(rg_client, subscription_ids, query, "unattached_nic")


def collect_unused_nsgs(rg_client, subscription_ids: list) -> list:
    query = """
    resources
    | where type =~ 'microsoft.network/networksecuritygroups'
    | where array_length(properties.networkInterfaces) == 0
        and array_length(properties.subnets) == 0
    | project id, name, resourceGroup, location
    """
    return _run_query(rg_client, subscription_ids, query, "unused_nsg")


def collect_aging_snapshots(rg_client, subscription_ids: list, older_than_days: int = 90) -> list:
    query = f"""
    resources
    | where type =~ 'microsoft.compute/snapshots'
    | extend createdTime = todatetime(properties.timeCreated)
    | where createdTime < ago({older_than_days}d)
    | project id, name, resourceGroup, location,
              diskSizeGb = toint(properties.diskSizeGb),
              timeCreated = tostring(properties.timeCreated)
    """
    return _run_query(rg_client, subscription_ids, query, "aging_snapshot")


def collect_stopped_not_deallocated_vms(rg_client, subscription_ids: list) -> list:
    resource_type, condition = ZOMBIE_CONDITIONS["stopped_not_deallocated"]
    query = f"""
    resources
    | where type =~ '{resource_type}'
    {condition}
    | project id, name, resourceGroup, location, powerState,
              vmSize = tostring(properties.hardwareProfile.vmSize)
    """
    return _run_query(rg_client, subscription_ids, query, "stopped_not_deallocated")


def collect_empty_load_balancers(rg_client, subscription_ids: list) -> list:
    resource_type, condition = ZOMBIE_CONDITIONS["empty_load_balancer"]
    query = f"""
    resources
    | where type =~ '{resource_type}'
    {condition}
    | project id, name, resourceGroup, location,
              sku = tostring(sku.name)
    """
    return _run_query(rg_client, subscription_ids, query, "empty_load_balancer")


def collect_vms_missing_autoshutdown(rg_client, subscription_ids: list) -> list:
    """
    Cross-references VMs against DevTest Lab auto-shutdown schedules.
    A VM is flagged if it has no corresponding shutdown schedule AND
    is not tagged as production.
    """
    query = """
    resources
    | where type =~ 'microsoft.compute/virtualmachines'
    | extend envTag = tolower(tostring(tags.environment))
    | where envTag !in ('production', 'prod')
    | project id, name, resourceGroup, location, envTag,
              vmSize = tostring(properties.hardwareProfile.vmSize)
    """
    vms = _run_query(rg_client, subscription_ids, query, "no_autoshutdown_vms")

    schedule_query = """
    resources
    | where type =~ 'microsoft.devtestlab/schedules'
    | where name contains 'shutdown-computevm'
    | project targetResourceId = tostring(properties.targetResourceId)
    """
    schedules = _run_query(rg_client, subscription_ids, schedule_query, "no_autoshutdown_schedules")
    scheduled_ids = {s["targetResourceId"].lower() for s in schedules if s.get("targetResourceId")}

    return [vm for vm in vms if vm["id"].lower() not in scheduled_ids]


def collect_all_zombie_resources(ctx, subscription_ids: list) -> dict:
    """
    Runs all eight zombie-detection queries and returns them grouped
    by category. One ResourceGraphClient instance reused across all
    queries to minimize auth overhead. Individual query failures
    return empty lists rather than propagating — a bad query in one
    category never prevents the other seven from running.
    """
    rg_client = ResourceGraphClient(credential=ctx.credential)

    collectors = {
        "unattached_public_ip":    collect_unattached_public_ips,
        "orphaned_disk":           collect_orphaned_disks,
        "unattached_nic":          collect_unattached_nics,
        "unused_nsg":              collect_unused_nsgs,
        "aging_snapshot":          collect_aging_snapshots,
        "stopped_not_deallocated": collect_stopped_not_deallocated_vms,
        "empty_load_balancer":     collect_empty_load_balancers,
        "no_autoshutdown":         collect_vms_missing_autoshutdown,
    }

    results = {}
    for category, fn in collectors.items():
        try:
            items = fn(rg_client, subscription_ids)
            results[category] = items
            log.info(f"  Zombie scan — {category}: {len(items)} found")
        except Exception as exc:
            log.warning(f"  Zombie scan — {category}: failed ({exc})")
            results[category] = []

    return results
