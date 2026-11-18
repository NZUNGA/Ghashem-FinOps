"""
Cognitive Services security/governance posture analyzer — Session 5b,
widened same-day to cover every Cognitive Services kind (not just
OpenAI/AIServices).

Reads the resource list collector_ai.py's
_find_cognitive_services_resources() already discovered — every
Cognitive Services account regardless of kind (including the
governance fields it projects: disable_local_auth,
public_network_access, has_private_endpoint), raises up to three
posture findings per resource, and writes one AIResourcePosture row
per resource per pipeline run. Local auth / public network / private
endpoint are meaningful security questions for a Vision or Speech
resource just as much as an OpenAI one — this analyzer doesn't care
what the resource is used for, only how it's exposed.

Two fields on AIResourcePosture — content_filter_enabled and
defender_enabled — are always written as None here. Detecting either
needs a separate API call this phase deliberately doesn't make
(CognitiveServicesManagementClient.rai_policies.list() and the
Defender for Cloud API respectively — see CLAUDE.md Known Gaps).
required_tags_present and local_auth_disabled/public_network_disabled/
private_endpoint_present are the only controls this analyzer can
actually evaluate today, so posture_score is out of the same 6-control
denominator regardless — an unimplemented control counts as failing,
not as excluded.

No dollar savings story for any of these (same as analyzer_ai_waste.py)
— monthly_savings is always 0.
"""

import logging
from pipeline.finding_utils import upsert_finding

log = logging.getLogger(__name__)


def _check_required_tags(tags):
    if not tags:
        return False
    keys = {k.lower() for k in tags.keys()}
    return "costcenter" in keys and "owner" in keys


def _process_resource(client, run_obj, resource) -> int:
    """Raises up to 3 findings for one resource and writes its
    AIResourcePosture row. Returns the number of findings created."""
    created = 0

    resource_id = resource["id"]
    resource_name = resource.get("name", "")
    resource_group = resource.get("resourceGroup", "")
    # Real kind (openai, aiservices, computervision, speechservices,
    # etc.) — collector_ai.py now discovers every Cognitive Services
    # kind, not just OpenAI/AIServices, so this can no longer just
    # rely on AIResourcePosture.resource_type's "openai" model default.
    resource_type = (resource.get("kind") or "").lower() or "unknown"
    tags = resource.get("tags")
    disable_local_auth = resource.get("disable_local_auth")
    # publicNetworkAccess is a STRING enum ("Enabled"/"Disabled") per
    # ARM's schema, not a boolean — confirmed live against TC-Ghashem
    # (two different test resources both returned
    # publicNetworkAccess: 'Enabled'). The == "Disabled" comparison
    # below also correctly fails safe (treats as "not disabled") if
    # the property is ever None/unset, same as disable_local_auth's
    # `is True` check just below — one of those test resources had
    # disableLocalAuth set to None (never explicitly configured), which
    # is exactly the case that fail-safe handling is for.
    public_network_access = resource.get("public_network_access")
    has_private_endpoint = resource.get("has_private_endpoint", False)

    # 1. Local authentication (API keys)
    if disable_local_auth is not True:
        try:
            upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=resource_id,
                resource_name=resource_name,
                resource_group=resource_group,
                finding_type="ai_local_auth_enabled",
                analyzer="analyzer_ai_posture",
                severity="high",
                monthly_savings=0,
                evidence=(
                    f"Local authentication (API keys) is enabled on "
                    f"'{resource_name}'. Any caller with the API key "
                    f"bypasses Managed Identity."
                ),
                action=(
                    f"Disable local authentication: az cognitiveservices "
                    f"account update --name {resource_name} "
                    f"--resource-group {resource_group} "
                    f"--custom-domain {resource_name} "
                    f"--disable-local-auth true. Switch callers to "
                    f"Managed Identity."
                ),
            )
            created += 1
        except Exception as exc:
            log.warning(f"[{client.slug}] ai_local_auth_enabled finding "
                        f"failed for {resource_id}: {exc}")

    # 2. Public network access
    if public_network_access != "Disabled":
        try:
            upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=resource_id,
                resource_name=resource_name,
                resource_group=resource_group,
                finding_type="ai_public_network_access",
                analyzer="analyzer_ai_posture",
                severity="high",
                monthly_savings=0,
                evidence=(
                    f"'{resource_name}' is accessible from the public "
                    f"internet. publicNetworkAccess = "
                    f"'{public_network_access}'."
                ),
                action=(
                    "Disable public network access and configure a "
                    "Private Endpoint."
                ),
            )
            created += 1
        except Exception as exc:
            log.warning(f"[{client.slug}] ai_public_network_access finding "
                        f"failed for {resource_id}: {exc}")

    # 3. Private endpoint
    if not has_private_endpoint:
        try:
            upsert_finding(
                client=client, run_obj=run_obj,
                resource_id=resource_id,
                resource_name=resource_name,
                resource_group=resource_group,
                finding_type="ai_no_private_endpoint",
                analyzer="analyzer_ai_posture",
                severity="medium",
                monthly_savings=0,
                evidence=(
                    f"'{resource_name}' has no Private Endpoint. Traffic "
                    f"travels over public internet."
                ),
                action=(
                    "Create a Private Endpoint in your VNet with private "
                    "DNS zone privatelink.cognitiveservices.azure.com."
                ),
            )
            created += 1
        except Exception as exc:
            log.warning(f"[{client.slug}] ai_no_private_endpoint finding "
                        f"failed for {resource_id}: {exc}")

    # 4. Posture row — resource_id normalized the same way every other
    # analyzer/collector in this codebase normalizes it (lower/strip/
    # rstrip trailing slash), not just .lower(), to avoid the casing-
    # duplicate bug already fixed elsewhere this session.
    from core.models import AIResourcePosture
    from django.utils import timezone
    normalized_id = resource_id.lower().strip().rstrip("/")
    posture, _ = AIResourcePosture.objects.update_or_create(
        client=client,
        resource_id=normalized_id,
        pipeline_run=run_obj,
        defaults={
            "resource_name": resource_name,
            "resource_group": resource_group,
            "resource_type": resource_type,
            "local_auth_disabled": disable_local_auth is True,
            "public_network_disabled": public_network_access == "Disabled",
            "private_endpoint_present": has_private_endpoint,
            "required_tags_present": _check_required_tags(tags),
            "content_filter_enabled": None,
            "defender_enabled": None,
            # This resource just came back from a live Resource Graph
            # discovery call (collector_ai.py), so its existence is
            # reconfirmed right now — live_check.py's existence check
            # reads this to know how recently a resource was last seen.
            "last_seen_at": timezone.now(),
        },
    )
    posture.calculate_score()
    posture.save(update_fields=["posture_score"])

    return created


def run(client_obj, run_obj, ai_resources) -> int:
    """
    ai_resources: the raw resource list from collector_ai.py's
    _find_openai_resources() (returned as the second element of
    collector_ai.run()'s (rows_saved, resources) tuple).

    Returns the number of findings created/updated. AIResourcePosture
    rows written is not part of the return value — one row is always
    written per resource regardless of whether any finding fired, so
    it's not a meaningful "count" the way findings are; check
    AIResourcePosture.objects.count() directly if needed.
    """
    if not ai_resources:
        return 0

    created = 0
    for resource in ai_resources:
        try:
            created += _process_resource(client_obj, run_obj, resource)
        except Exception as exc:
            rid = resource.get("id", "?")
            log.warning(f"[{client_obj.slug}] AI posture check failed "
                        f"for {rid}: {exc}")

    return created
