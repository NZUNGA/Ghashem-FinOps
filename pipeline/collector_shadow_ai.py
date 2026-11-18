"""
Shadow AI discovery — Session 5b.

Named collector_* per the build spec, but — like
analyzer_ai_waste.py's untagged-resource check and
analyzer_reservations.py before it — it also writes Finding rows
directly rather than only raw data, since the whole point is a single
pass that both discovers AI-adjacent resource types beyond the
Azure OpenAI accounts collector_ai.py already tracks in DailyAIUsage,
and immediately flags the ones that look ungoverned. Splitting that
into a separate collect-then-analyze pair would mean persisting a new
raw-resource model for data that's only ever used once, right after
being fetched — not worth it for what's currently a single finding
type. Flagging this rather than silently renaming the file, since
CLAUDE.md's "Conventions" section documents collectors as read-only
Azure/write-raw-data and analyzers as DB-only/write-Finding.

Resource types scanned (broader than collector_ai.py's Cognitive
Services-only search): Cognitive Services accounts, AI Search
services, Bot Service bots, and Azure ML workspaces — anything a team
could stand up an AI capability on without it ever reaching the AI
Cost Intelligence / AI Governance pipeline collector_ai.py only looks
at Cognitive Services `kind == 'OpenAI'` for.

A resource is flagged shadow_ai_resource only if BOTH:
  1. its resource_id is not already known via DailyAIUsage (i.e. not
     already being tracked by the governed AI Cost Intelligence path), AND
  2. it has neither a CostCenter nor an Owner tag.
A resource missing from DailyAIUsage but properly tagged is treated as
a deliberate, attributable provisioning choice, not shadow usage —
only the combination of "unknown to the pipeline" AND "no cost
attribution" is genuinely suspicious.
"""

import logging
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from core.models import DailyAIUsage
from pipeline.finding_utils import upsert_finding

log = logging.getLogger(__name__)

AI_ADJACENT_TYPES = [
    "microsoft.cognitiveservices/accounts",
    "microsoft.search/searchservices",
    "microsoft.botservice/botservices",
    "microsoft.machinelearningservices/workspaces",
]


def _find_ai_adjacent_resources(rg_client: ResourceGraphClient, subscription_ids: list) -> list:
    # 'id' is required to identify each resource and check it against
    # DailyAIUsage — added to the projection (not part of the original
    # spec, which omitted it).
    types_list = ", ".join(f"'{t}'" for t in AI_ADJACENT_TYPES)
    query = f"""
    Resources
    | where type in ({types_list})
    | project
        id, name, type, kind, resourceGroup,
        location, tags, subscriptionId,
        disableLocalAuth = properties.disableLocalAuth,
        publicNetworkAccess = properties.publicNetworkAccess
    """
    try:
        request = QueryRequest(subscriptions=subscription_ids, query=query)
        result = rg_client.resources(request)
        return list(result.data)
    except Exception as exc:
        log.warning(f"  Shadow AI discovery query failed: {exc}")
        return []


def run(client_obj, run_obj, ctx) -> int:
    """
    Scans for AI-adjacent resources not already governed via
    DailyAIUsage and lacking cost-attribution tags. Returns the number
    of shadow_ai_resource findings created/updated. Never raises — a
    failure here must never fail a pipeline run that otherwise
    completed successfully.
    """
    try:
        subscription_ids = [ctx.subscription_id]
        rg_client = ResourceGraphClient(credential=ctx.credential)
        resources = _find_ai_adjacent_resources(rg_client, subscription_ids)
        if not resources:
            return 0

        known_ids_lower = {
            rid.lower().strip().rstrip("/")
            for rid in DailyAIUsage.objects.filter(client=client_obj)
            .values_list("resource_id", flat=True)
        }

        created = 0
        for res in resources:
            resource_id = res.get("id")
            if not resource_id:
                continue

            try:
                # Deliberately AND, not OR: flag as shadow_ai_resource
                # only when BOTH conditions hold (untracked AND
                # untagged), not either alone. "Shadow AI" means
                # invisible to governance entirely — a resource already
                # flowing into DailyAIUsage IS visible/monitored, even
                # if it happens to be missing tags; that's a distinct,
                # already-covered problem
                # (analyzer_ai_waste.py's untagged_ai_resource finding).
                # Flagging every known-but-untagged resource as ALSO
                # "shadow" here would just duplicate that finding under
                # a different name and dilute what "shadow" is supposed
                # to mean. Confirmed against a live case: a test resource
                # was tracked (had DailyAIUsage rows) but untagged — it
                # correctly got untagged_ai_resource only, never
                # shadow_ai_resource, because it was known to the pipeline.
                rid_lower = resource_id.lower().strip().rstrip("/")
                if rid_lower in known_ids_lower:
                    continue  # already governed via the AI Cost Intelligence path

                tags = res.get("tags") or {}
                tag_keys_lower = {k.lower() for k in tags.keys()}
                if "costcenter" in tag_keys_lower and "owner" in tag_keys_lower:
                    continue  # untracked but attributable — not shadow usage

                name = res.get("name", "")
                resource_type = res.get("type", "")
                resource_group = res.get("resourceGroup", "")

                upsert_finding(
                    client=client_obj, run_obj=run_obj,
                    resource_id=resource_id,
                    resource_name=name,
                    resource_group=resource_group,
                    finding_type="shadow_ai_resource",
                    analyzer="collector_shadow_ai",
                    severity="medium",
                    monthly_savings=0,
                    evidence=(
                        f"AI resource '{name}' ({resource_type}) was "
                        f"detected but has not been onboarded through AI "
                        f"governance. It has no CostCenter or Owner tag."
                    ),
                    action=(
                        "Review this resource. If legitimate, add "
                        "governance tags and onboard it to the AI "
                        "tracking pipeline. If unauthorized, delete it."
                    ),
                )
                created += 1
            except Exception as exc:
                log.warning(f"[{client_obj.slug}] shadow_ai_resource "
                            f"finding failed for {resource_id}: {exc}")

        return created

    except Exception as exc:
        slug = getattr(client_obj, "slug", "?")
        log.warning(f"[{slug}] Shadow AI scan failed entirely: {exc}")
        return 0
