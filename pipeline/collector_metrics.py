import time
import logging
from datetime import date, datetime, timezone, timedelta
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from core.models import DailyMetric, Client

log = logging.getLogger(__name__)

RESOURCE_TYPE_MAP = {
    "microsoft.cognitiveservices/accounts":          "Microsoft.CognitiveServices/accounts",
    "microsoft.compute/virtualmachines":             "Microsoft.Compute/virtualMachines",
    "microsoft.storage/storageaccounts":             "Microsoft.Storage/storageAccounts",
    "microsoft.web/sites":                           "Microsoft.Web/sites",
    "microsoft.sql/servers/databases":               "Microsoft.Sql/servers/databases",
    "microsoft.cache/redis":                         "Microsoft.Cache/Redis",
    "microsoft.dbforpostgresql/flexibleservers":     "Microsoft.DBforPostgreSQL/flexibleServers",
    "microsoft.containerservice/managedclusters":    "Microsoft.ContainerService/managedClusters",
}

METRIC_DEFINITIONS = {
    "Microsoft.CognitiveServices/accounts": [
        {"name": "TotalCalls",            "aggregation": "Total"},
        {"name": "SuccessfulCalls",        "aggregation": "Total"},
        {"name": "TotalErrors",            "aggregation": "Total"},
        {"name": "BlockedCalls",           "aggregation": "Total"},
        {"name": "Latency",                "aggregation": "Average"},
        {"name": "ProcessedPromptTokens",  "aggregation": "Total"},
        {"name": "GeneratedTokens",        "aggregation": "Total"},
        # TokenTransaction is the Azure OpenAI provisioned-throughput (PTU)
        # metric — Azure OpenAI deployments are Microsoft.CognitiveServices/
        # accounts resources distinguished by `kind`, not a separate ARM
        # resource type, so this belongs on this namespace rather than a
        # new one (there is no such Resource Graph type as
        # "Microsoft.CognitiveServices/accounts/OpenAI").
        {"name": "TokenTransaction",       "aggregation": "Total"},
    ],
    "Microsoft.Compute/virtualMachines": [
        {"name": "Percentage CPU",         "aggregation": "Average"},
        {"name": "Available Memory Bytes", "aggregation": "Average"},
        {"name": "Network In Total",       "aggregation": "Total"},
        {"name": "Network Out Total",      "aggregation": "Total"},
    ],
    "Microsoft.Storage/storageAccounts": [
        {"name": "UsedCapacity",           "aggregation": "Average"},
        {"name": "Transactions",           "aggregation": "Total"},
    ],
    # Web App metrics â each fetched individually since they have
    # different aggregation types and interval requirements.
    # CpuTime uses PT1H (hourly) because P1D returns no data for it.
    "Microsoft.Web/sites": [
        {"name": "Requests",               "aggregation": "Total",   "interval": "P1D"},
        {"name": "MemoryWorkingSet",       "aggregation": "Average", "interval": "P1D"},
        {"name": "CpuTime",                "aggregation": "Total",   "interval": "PT1H"},
        {"name": "Http5xx",                "aggregation": "Total",   "interval": "P1D"},
    ],
    "Microsoft.Sql/servers/databases": [
        {"name": "dtu_consumption_percent", "aggregation": "Average"},
    ],
    "Microsoft.Cache/Redis": [
        {"name": "usedmemory_percentage",   "aggregation": "Average"},
    ],
    "Microsoft.DBforPostgreSQL/flexibleServers": [
        {"name": "cpu_percent",             "aggregation": "Average"},
    ],
    "Microsoft.ContainerService/managedClusters": [
        {"name": "node_cpu_usage_percentage", "aggregation": "Average"},
    ],
}

CHUNK_DAYS = 29
# Batch size for bulk_create â keeps memory usage bounded for large tenants
BATCH_SIZE = 500


def _chunks(start: date, end: date):
    current = start
    while current < end:
        yield current, min(current + timedelta(days=CHUNK_DAYS), end)
        current = current + timedelta(days=CHUNK_DAYS)


def collect_inventory(ctx, subscription_ids: list) -> list:
    """Returns list of dicts with resource metadata."""
    rg_client = ResourceGraphClient(credential=ctx.credential)
    types = list(RESOURCE_TYPE_MAP.keys())
    type_filter = " or ".join([f"type =~ '{t}'" for t in types])
    query = f"""
    resources
    | where {type_filter}
    | project id, name, type, resourceGroup, subscriptionId, location, tags,
              resourceKind = tostring(kind)
    | order by type asc
    """
    result = rg_client.resources(QueryRequest(subscriptions=subscription_ids, query=query))
    resources = []
    for row in result.data:
        row["metric_namespace"] = RESOURCE_TYPE_MAP.get(row.get("type", "").lower(), "")
        row["kind"] = row.pop("resourceKind", "")
        resources.append(row)
    return resources


def collect_metrics(client_obj: Client, ctx, resources: list,
                    start_date: date, end_date: date) -> int:
    """
    Collects daily metrics for all known resource types and bulk-writes
    them to DailyMetric in batches. Using bulk_create instead of
    per-row update_or_create eliminates the SQLite lock contention
    that caused "database is locked" errors when the pipeline ran in
    a background thread alongside Django's request handling.

    Web App metrics are fetched metric-by-metric rather than as a
    comma-separated batch, since CpuTime requires a different interval
    (PT1H) from the other metrics (P1D).
    """
    monitor = MonitorManagementClient(
        credential=ctx.credential,
        subscription_id=ctx.subscription_id,
    )
    saved = 0
    pending = []

    def flush(force=False):
        nonlocal saved, pending
        if not pending or (not force and len(pending) < BATCH_SIZE):
            return
        # Deduplicate pending rows â same (client, resource_id, metric_name, date)
        # appearing twice in one batch causes PostgreSQL to error even with
        # update_conflicts=True. Keep the last value for each key.
        seen = {}
        for row in pending:
            key = (row.client_id, row.resource_id, row.metric_name, row.date)
            seen[key] = row
        pending = list(seen.values())

        DailyMetric.objects.bulk_create(
            pending,
            update_conflicts=True,
            update_fields=[
                "resource_name", "resource_group", "metric_namespace",
                "value", "aggregation",
            ],
            unique_fields=["client", "resource_id", "metric_name", "date"],
        )
        saved += len(pending)
        pending.clear()

    for res in resources:
        namespace     = res.get("metric_namespace", "")
        metrics_config = METRIC_DEFINITIONS.get(namespace, [])
        if not metrics_config:
            continue

        # Web App: fetch each metric individually (different intervals)
        # Other types: batch all metrics in one API call per chunk
        if namespace == "Microsoft.Web/sites":
            for metric_def in metrics_config:
                interval = metric_def.get("interval", "P1D")
                for chunk_start, chunk_end in _chunks(start_date, end_date):
                    s = datetime(chunk_start.year, chunk_start.month,
                                 chunk_start.day, tzinfo=timezone.utc)
                    e = datetime(chunk_end.year, chunk_end.month,
                                 chunk_end.day, tzinfo=timezone.utc)
                    try:
                        resp = monitor.metrics.list(
                            resource_uri=res["id"],
                            timespan=f"{s.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
                                     f"{e.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                            interval=interval,
                            metricnames=metric_def["name"],
                            metricnamespace=namespace,
                            aggregation=metric_def["aggregation"],
                        )
                        for metric in resp.value:
                            for ts in metric.timeseries:
                                for point in ts.data:
                                    val = getattr(
                                        point,
                                        metric_def["aggregation"].lower(),
                                        None,
                                    )
                                    pending.append(DailyMetric(
                                        client=client_obj,
                                        resource_id=res["id"].lower().strip().rstrip("/"),
                                        resource_name=res.get("name", "")[:300],
                                        resource_group=res.get("resourceGroup", "")[:300],
                                        metric_namespace=namespace,
                                        metric_name=metric.name.value,
                                        date=point.time_stamp.date(),
                                        value=val,
                                        aggregation=metric_def["aggregation"],
                                    ))
                        flush()
                    except Exception as exc:
                        log.warning(
                            f"Metric pull failed for {res.get('name')} "
                            f"[{metric_def['name']}] "
                            f"{chunk_start}â{chunk_end}: {exc}"
                        )
                    time.sleep(0.2)
        else:
            metric_names = ",".join(m["name"] for m in metrics_config)
            agg_types    = ",".join(sorted(set(m["aggregation"] for m in metrics_config)))
            agg_map      = {m["name"]: m["aggregation"] for m in metrics_config}

            for chunk_start, chunk_end in _chunks(start_date, end_date):
                s = datetime(chunk_start.year, chunk_start.month,
                             chunk_start.day, tzinfo=timezone.utc)
                e = datetime(chunk_end.year, chunk_end.month,
                             chunk_end.day, tzinfo=timezone.utc)
                try:
                    resp = monitor.metrics.list(
                        resource_uri=res["id"],
                        timespan=f"{s.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
                                 f"{e.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                        interval="P1D",
                        metricnames=metric_names,
                        metricnamespace=namespace,
                        aggregation=agg_types,
                    )
                    for metric in resp.value:
                        agg = agg_map.get(metric.name.value, "Average")
                        for ts in metric.timeseries:
                            for point in ts.data:
                                val = getattr(point, agg.lower(), None)
                                pending.append(DailyMetric(
                                    client=client_obj,
                                    resource_id=res["id"].lower().strip().rstrip("/"),
                                    resource_name=res.get("name", "")[:300],
                                    resource_group=res.get("resourceGroup", "")[:300],
                                    metric_namespace=namespace,
                                    metric_name=metric.name.value,
                                    date=point.time_stamp.date(),
                                    value=val,
                                    aggregation=agg,
                                ))
                    flush()
                except Exception as exc:
                    log.warning(
                        f"Metric pull failed for {res.get('name')} "
                        f"{chunk_start}â{chunk_end}: {exc}"
                    )
                time.sleep(0.3)

    flush(force=True)
    log.info(f"[{client_obj.slug}] Metric rows saved: {saved}")
    return saved

