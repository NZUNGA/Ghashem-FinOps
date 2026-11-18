"""
Azure Cognitive Services discovery + Azure OpenAI token usage collector.

Discovers EVERY Cognitive Services account in ctx's subscription,
regardless of kind (Vision, Speech, Translation, Face, FormRecognizer,
OpenAI, AIServices, etc. — see `az cognitiveservices account
list-kinds` for the full set, checked live against TC-Ghashem) via
Resource Graph (same ResourceGraphClient pattern as
collector_zombie.py). Widened from an OpenAI/AIServices-only scope so
pipeline/analyzer_ai_posture.py's governance checks (local auth,
public network, private endpoint, tags) cover every Cognitive Services
resource a client has, not just the token-billed ones — those checks
are meaningful no matter what the service actually does.

Token-usage collection (pulling daily token metrics from Azure Monitor
and writing DailyAIUsage rows) still only runs for kind in
TOKEN_TRACKED_KINDS — every other kind is billed per-transaction, not
per-token, and never produces PRIMARY_METRICS/FALLBACK_METRICS data
regardless, so attempting it would just be a wasted Monitor API call.
Their idle/cost coverage instead comes from
pipeline/analyzer_resource_utilization.py's Microsoft.CognitiveServices/
accounts profile (TotalCalls-based, already collected for every kind
by collector_metrics.py with no kind filter), and their token-ratio
efficiency (for any kind that does happen to produce prompt/generated
token metrics) from pipeline/analyzer_token.py.

Metric names differ by model generation and aren't reliably knowable
in advance, so two metric sets are tried in order: PRIMARY (GPT-5 /
newer models) first, falling back to FALLBACK (GPT-4 / legacy models,
including TokenTransaction — the one metric independently confirmed
working against a live test deployment) if PRIMARY comes back empty
or errors. Both sets are also tried with a
ModelDeploymentName dimension split first (see DIMENSION_NAME), falling
back to resource-level (unsplit, deployment_name="") if the dimension
isn't supported.

Never raises — a collector failure here must never fail the pipeline
run that otherwise completed successfully.
"""

import logging
from datetime import datetime, timezone, timedelta
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from core.models import DailyAIUsage

log = logging.getLogger(__name__)

# USD per million tokens — August 2026 approximations. (input price, output price)
AI_PRICING = {
    "gpt-5-nano":   (0.05,  0.40),
    "gpt-5-mini":   (0.15,  0.60),
    "gpt-5":        (2.00,  8.00),
    "gpt-5-pro":    (8.00, 32.00),
    "gpt-4o-mini":  (0.15,  0.60),
    "gpt-4o":       (2.50, 10.00),
    "gpt-4.1":      (2.00,  8.00),
    "gpt-4.1-mini": (0.40,  1.60),
    "gpt-4.1-nano": (0.10,  0.40),
    "default":      (1.00,  4.00),
}

PRIMARY_METRICS  = {"input": "InputTokens",           "output": "OutputTokens",  "total": "TotalTokens"}
FALLBACK_METRICS = {"input": "ProcessedPromptTokens",  "output": "GeneratedTokens", "total": "TokenTransaction"}

# Kinds that support token-based billing/metrics and the deployments
# API — confirmed against the full `az cognitiveservices account
# list-kinds` output (22 kinds total). Everything else (ComputerVision,
# SpeechServices, TextTranslation, Face, FormRecognizer, CustomVision.*,
# the plain multi-service CognitiveServices kind, etc.) is billed
# per-transaction/per-character/per-minute and never produces
# PRIMARY_METRICS/FALLBACK_METRICS data — checked lowercase since
# Resource Graph's `kind` casing isn't guaranteed consistent.
TOKEN_TRACKED_KINDS = {"openai", "aiservices"}

COLLECTION_DAYS = 30
# Azure OpenAI / Cognitive Services metrics expose the deployment as
# "ModelDeploymentName", not "DeploymentName" — confirmed wrong on the
# first live run against a test deployment (every row came back with
# deployment_name="", meaning the filter matched nothing and the code
# fell back to its unsplit resource-level path). Kept _fetch_metric_series's
# graceful fallback either way, in case this still isn't the real name
# for some metric/resource combination.
DIMENSION_NAME  = "ModelDeploymentName"


def _estimate_cost(model_name: str, input_tok: int, output_tok: int) -> float:
    key = (model_name or "").lower()
    prices = None
    for k, v in AI_PRICING.items():
        if k != "default" and key.startswith(k):
            prices = v
            break
    if not prices:
        prices = AI_PRICING["default"]
    input_price, output_price = prices
    return (input_tok / 1_000_000 * input_price) + (output_tok / 1_000_000 * output_price)


def _find_cognitive_services_resources(rg_client: ResourceGraphClient, subscription_ids: list) -> list:
    """
    Renamed from _find_openai_resources — now discovers EVERY
    Cognitive Services account regardless of kind, not just
    OpenAI/AIServices (see module docstring). Only called from within
    this file, so the rename is safe.

    Governance properties (disableLocalAuth, publicNetworkAccess,
    networkAcls, hasPrivateEndpoint) are projected here for
    pipeline/analyzer_ai_posture.py — added Session 5b, alongside the
    original id/name/resourceGroup/subscriptionId/location/tags fields
    this collector's own DailyAIUsage rows still use, plus `kind`
    (added when discovery widened to all kinds) so callers can tell
    which resources are actually token-trackable. Each row also gets
    snake_case aliases (disable_local_auth, public_network_access,
    has_private_endpoint) added on top of the raw camelCase Resource
    Graph fields, since the posture analyzer reads those.
    """
    query = """
    Resources
    | where type == 'microsoft.cognitiveservices/accounts'
    | project
        id, name, kind, resourceGroup, subscriptionId, location, tags,
        disableLocalAuth = properties.disableLocalAuth,
        publicNetworkAccess = properties.publicNetworkAccess,
        networkAcls = properties.networkAcls,
        hasPrivateEndpoint = (
            array_length(properties.privateEndpointConnections) > 0
        )
    """
    try:
        request = QueryRequest(subscriptions=subscription_ids, query=query)
        result = rg_client.resources(request)
        resources = []
        for row in result.data:
            row["disable_local_auth"] = row.get("disableLocalAuth")
            row["public_network_access"] = row.get("publicNetworkAccess")
            row["has_private_endpoint"] = row.get("hasPrivateEndpoint", False)
            resources.append(row)
        return resources
    except Exception as exc:
        log.warning(f"  Cognitive Services discovery query failed: {exc}")
        return []


def _fetch_metric_series(monitor: MonitorManagementClient, resource_id: str, metric_name: str,
                         start_dt: datetime, end_dt: datetime, use_dimension: bool = True):
    """
    Returns {deployment_name: {date: value}} for one metric, or None if
    the metric produced no usable data at all (so the caller can fall
    back to a different metric name). deployment_name is "" whenever
    the dimension split isn't used or didn't return anything — resource-
    level totals are still real data, just not broken out per deployment.
    """
    kwargs = dict(
        resource_uri=resource_id,
        timespan=f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        interval="P1D",
        metricnames=metric_name,
        aggregation="Total",
    )
    if use_dimension:
        kwargs["filter"] = f"{DIMENSION_NAME} eq '*'"

    try:
        resp = monitor.metrics.list(**kwargs)
    except Exception as exc:
        if use_dimension:
            log.info(f"  Dimension-filtered call failed for {metric_name} "
                     f"on {resource_id}, retrying without split: {exc}")
            return _fetch_metric_series(monitor, resource_id, metric_name,
                                        start_dt, end_dt, use_dimension=False)
        log.info(f"  Metric {metric_name} unavailable for {resource_id}: {exc}")
        return None

    by_deployment = {}
    saw_any_point = False
    for metric in resp.value:
        for ts in metric.timeseries:
            deployment_name = ""
            # SDK attribute is "metadatavalues" (no underscore) on
            # TimeSeriesElement — confirmed against azure.mgmt.monitor.
            # models.TimeSeriesElement._attribute_map. Using the wrong
            # (snake_case) name here silently found nothing every time,
            # independent of whether DIMENSION_NAME itself was correct.
            if use_dimension and getattr(ts, "metadatavalues", None):
                for mv in ts.metadatavalues:
                    if mv.name and mv.name.value and mv.name.value.lower() == DIMENSION_NAME.lower():
                        deployment_name = mv.value or ""
                        break
            for point in ts.data:
                val = point.total
                if val is None:
                    continue
                saw_any_point = True
                d = point.time_stamp.date()
                by_deployment.setdefault(deployment_name, {})
                by_deployment[deployment_name][d] = by_deployment[deployment_name].get(d, 0) + val

    if not saw_any_point and use_dimension:
        # Dimension filter returned nothing (unsupported dimension, or
        # genuinely no per-deployment breakdown available) — retry
        # unsplit before giving up on this metric entirely.
        return _fetch_metric_series(monitor, resource_id, metric_name,
                                    start_dt, end_dt, use_dimension=False)

    return by_deployment if saw_any_point else None


def _deployment_model_names(ctx, resource_group: str, account_name: str) -> dict:
    """
    Returns {deployment_name: model_name} for one Cognitive Services
    account via the real deployments-list API — e.g. deployment
    "gpt-5-nano-test" -> model "gpt-5-nano". Confirmed against
    azure.mgmt.cognitiveservices.models.Deployment/DeploymentModel:
    Deployment.name is the deployment, Deployment.properties.model.name
    is the underlying model. Returns {} on any failure so the caller
    falls back to approximating model_name from deployment_name.
    """
    try:
        cog_client = CognitiveServicesManagementClient(
            credential=ctx.credential,
            subscription_id=ctx.subscription_id,
        )
        result = {}
        for deployment in cog_client.deployments.list(resource_group, account_name):
            model = getattr(deployment.properties, "model", None) if deployment.properties else None
            model_name = getattr(model, "name", None) if model else None
            if model_name:
                result[deployment.name] = model_name
        return result
    except Exception as exc:
        log.info(f"  Deployment model lookup failed for {account_name}: {exc}")
        return {}


def _collect_metric_set(monitor: MonitorManagementClient, resource_id: str, metrics: dict):
    """
    metrics: {"input": name, "output": name, "total": name}.
    Returns {deployment_name: {date: {"input", "output", "total"}}}, or
    None if the total-tokens metric produced no real (non-zero) data —
    the signal the caller uses to try the next metric set.
    """
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=COLLECTION_DAYS)

    total_series = _fetch_metric_series(monitor, resource_id, metrics["total"], start_dt, end_dt)
    if not total_series or not any(any(v > 0 for v in days.values()) for days in total_series.values()):
        return None

    input_series = _fetch_metric_series(monitor, resource_id, metrics["input"], start_dt, end_dt) or {}
    output_series = _fetch_metric_series(monitor, resource_id, metrics["output"], start_dt, end_dt) or {}

    combined = {}
    for deployment_name, days in total_series.items():
        combined.setdefault(deployment_name, {})
        for d, total_val in days.items():
            combined[deployment_name].setdefault(d, {"input": 0, "output": 0, "total": 0})
            combined[deployment_name][d]["total"] = total_val

    for source_key, source_series in (("input", input_series), ("output", output_series)):
        for deployment_name, days in source_series.items():
            combined.setdefault(deployment_name, {})
            for d, val in days.items():
                combined[deployment_name].setdefault(d, {"input": 0, "output": 0, "total": 0})
                combined[deployment_name][d][source_key] = val

    return combined


def run(client_obj, run_obj, ctx) -> tuple:
    """
    Discovers every Cognitive Services account (any kind) in ctx's
    subscription, then collects Azure Monitor token usage only for the
    kinds in TOKEN_TRACKED_KINDS (OpenAI, AIServices).

    Returns (rows_saved, resources) — rows_saved is the DailyAIUsage
    row count (as before Session 5b); resources is the FULL resource
    list from _find_cognitive_services_resources() (every kind,
    including the governance fields it projects), so
    pipeline/runner.py can pass it straight into
    analyzer_ai_posture.run() for governance coverage across every
    Cognitive Services resource, not just the token-tracked ones —
    without a second Resource Graph discovery call.
    """
    try:
        # ctx (core.auth.ClientContext) is scoped to a single
        # subscription_id, same as collector_metrics.collect_metrics —
        # matches the wiring in pipeline/runner.py, which only has one
        # ctx in scope at the point this collector is called.
        subscription_ids = [ctx.subscription_id]
        rg_client = ResourceGraphClient(credential=ctx.credential)
        resources = _find_cognitive_services_resources(rg_client, subscription_ids)
        if not resources:
            log.info(f"[{client_obj.slug}] No Cognitive Services resources found")
            return 0, []

        monitor = MonitorManagementClient(
            credential=ctx.credential,
            subscription_id=ctx.subscription_id,
        )

        saved = 0
        for res in resources:
            kind = (res.get("kind") or "").lower()
            if kind not in TOKEN_TRACKED_KINDS:
                # Not a token-billed kind — no DailyAIUsage rows for it,
                # but it's still in `resources` (returned below) for
                # analyzer_ai_posture.py's governance checks, and its
                # idle/cost coverage comes from
                # analyzer_resource_utilization.py's TotalCalls-based
                # profile instead.
                continue

            resource_id = res["id"]
            resource_name = res.get("name", "")
            resource_group = res.get("resourceGroup", "")

            combined = None
            try:
                combined = _collect_metric_set(monitor, resource_id, PRIMARY_METRICS)
            except Exception as exc:
                log.warning(f"[{client_obj.slug}] Primary AI metrics failed "
                            f"for {resource_name}: {exc}")

            if not combined:
                try:
                    combined = _collect_metric_set(monitor, resource_id, FALLBACK_METRICS)
                except Exception as exc:
                    log.warning(f"[{client_obj.slug}] Fallback AI metrics failed "
                                f"for {resource_name}: {exc}")

            if not combined:
                log.info(f"[{client_obj.slug}] No AI token metrics available for {resource_name}")
                continue

            # One deployments-list call per resource (not per-deployment)
            # to get the real underlying model name for each deployment
            # found in the metrics above — e.g. deployment
            # "gpt-5-nano-test" -> model "gpt-5-nano". Falls back to the
            # deployment name itself if the lookup fails or a specific
            # deployment isn't in the result (e.g. since deleted).
            deployment_models = _deployment_model_names(ctx, resource_group, resource_name)

            for deployment_name, days in combined.items():
                model_name = deployment_models.get(deployment_name) or deployment_name
                for d, vals in days.items():
                    input_tok = int(vals["input"])
                    output_tok = int(vals["output"])
                    total_tok = int(vals["total"]) or (input_tok + output_tok)
                    cost = _estimate_cost(model_name, input_tok, output_tok)

                    DailyAIUsage.objects.update_or_create(
                        client=client_obj,
                        resource_id=resource_id.lower(),
                        deployment_name=deployment_name,
                        date=d,
                        defaults={
                            "pipeline_run": run_obj,
                            "resource_name": resource_name,
                            "resource_group": resource_group,
                            "model_name": model_name,
                            "input_tokens": input_tok,
                            "output_tokens": output_tok,
                            "total_tokens": total_tok,
                            "estimated_cost_usd": cost,
                        },
                    )
                    saved += 1

        log.info(f"[{client_obj.slug}] AI usage rows saved: {saved}")
        return saved, resources

    except Exception as exc:
        slug = getattr(client_obj, "slug", "?")
        log.warning(f"[{slug}] AI collector failed entirely: {exc}")
        return 0, []
