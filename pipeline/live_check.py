"""
Lightweight live-existence check.

Unlike the full pipeline (which re-collects cost, metrics, and runs
every analyzer — typically 10-30 minutes), this checks two things:

  1. For metered resources (from DailyMetric): does each resource we
     know about still exist in Azure right now, via a single batched
     generic Resource Graph query.
  2. For open zombie-type findings (orphaned disks, unattached IPs,
     empty LBs, stopped-not-deallocated VMs): is the resource STILL in
     the zombie state, via a targeted re-run of that category's own
     KQL condition (from collector_zombie.py), scoped to just the
     resource IDs with an open finding.

These are two separate passes because zombie-type resources mostly
have no DailyMetric rows at all (nothing to measure on an orphaned
disk), so pass 1 never sees them, and the generic `resources` table
pass 1 relies on doesn't reliably index all these types anyway —
pass 2 exists specifically to give those findings a real update path
instead of leaving them open forever between full pipeline runs.

Updates two models:
  - ResourceStatus — the source of truth the dashboard reads to
    decide whether to show a resource at all (All Resources page).
    Only written by pass 1 (metered resources).
  - Finding.status — marks open findings 'resolved' if their resource
    is confirmed gone (pass 1) or no longer in the zombie state
    (pass 2, e.g. VM restarted, disk reattached, IP assigned, LB got
    backend members).

Run this frequently (e.g. every few hours via a scheduled task) between
full pipeline runs, so the dashboard never shows a resource the client
already deleted or fixed — which is exactly the kind of "why does it
still say this if I fixed it" moment that erodes trust.
"""

import logging
from django.utils import timezone
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from core.models import Client, Finding, DailyMetric, ResourceStatus, AIResourcePosture
from pipeline import collector_zombie

log = logging.getLogger(__name__)

BATCH_SIZE = 200

# Zombie categories pass 2 knows how to re-verify — must match keys in
# collector_zombie.ZOMBIE_CONDITIONS. unattached_nic, unused_nsg, and
# aging_snapshot don't have a recheck condition defined (yet) and are
# left to pass 1's blanket protection, same as before this fix.
ZOMBIE_RECHECK_TYPES = set(collector_zombie.ZOMBIE_CONDITIONS.keys())

# Finding types excluded from live-check's existence-based auto-resolve.
# analyzer_anomaly's cost_anomaly finding is keyed on a synthetic
# per-client resource_id, not an ARM resource, so there is nothing for
# either live-check pass to query for existence — excluded defensively
# in case a synthetic resource_id ever collides with something in
# DailyMetric. analyzer_reservations' reservation_opportunity finding
# DOES have a real resource_id, but its validity is tied to whether
# Azure Advisor still recommends the reservation on the next full
# pipeline run, not whether the resource currently exists — a VM that
# still exists can stop being reservation-worthy for reasons live-check
# has no way to detect (already reserved, usage pattern changed, etc.),
# so only a fresh analyzer_reservations.run() should ever resolve it.
# idle_ai_deployment/ai_cost_spike/untagged_ai_resource are the same
# kind of exception as reservation_opportunity above — real resource_id,
# but validity depends on analyzer_ai_waste.run()'s next verdict (token
# usage, usage pattern, tags), not on whether the resource still exists.
# The 5 AI posture/shadow-AI types (Session 5b, analyzer_ai_posture.py /
# collector_shadow_ai.py) are the same kind of exception again — real
# resource_id, but validity depends on a live configuration/discovery
# check (local auth, network access, private endpoint, tags), not on
# whether the resource still exists.
NON_RESOURCE_FINDING_TYPES = {
    "cost_anomaly", "reservation_opportunity",
    "idle_ai_deployment", "ai_cost_spike", "untagged_ai_resource",
    "ai_local_auth_enabled", "ai_public_network_access",
    "ai_no_content_filter", "ai_no_private_endpoint",
    "shadow_ai_resource",
}

# Subset of NON_RESOURCE_FINDING_TYPES that CAN be resolved by pass 1's
# existence check, specifically when that check has just reconfirmed the
# resource is genuinely gone (not merely "not re-flagged this run" — the
# distinction the exclusion above exists to protect against). A deleted
# Cognitive Services account cannot meaningfully have "public network
# access enabled" or be an "ungoverned shadow resource" any more than a
# deleted VM can be "stopped but not deallocated". reservation_opportunity/
# idle_ai_deployment/ai_cost_spike/untagged_ai_resource stay excluded even
# here — their validity depends on the next analyzer_reservations/
# analyzer_ai_waste run re-evaluating usage patterns, not on existence
# alone, so only a fresh analyzer run should resolve those.
AI_POSTURE_FINDING_TYPES = {
    "ai_local_auth_enabled", "ai_public_network_access",
    "ai_no_content_filter", "ai_no_private_endpoint",
    "shadow_ai_resource",
}


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _existing_resource_ids(rg_client: ResourceGraphClient,
                           subscription_ids: list, resource_ids: list) -> set:
    """
    Returns the subset of resource_ids that still exist, queried in
    batches via the generic Resource Graph `resources` table.
    Normalizes to lowercase on both sides.
    """
    existing = set()

    for batch in _chunks(resource_ids, BATCH_SIZE):
        id_list = ", ".join(f"'{rid.lower()}'" for rid in batch)
        query = f"""
        resources
        | where tolower(id) in ({id_list})
        | project id
        """
        request = QueryRequest(subscriptions=subscription_ids, query=query)
        result = rg_client.resources(request)
        for row in result.data:
            existing.add(row["id"].lower())

    return existing


def _recheck_zombie_findings(rg_client: ResourceGraphClient, subscription_ids: list,
                             client_obj: Client, now) -> dict:
    """
    Pass 2 — re-verifies open zombie-type findings directly, since pass 1
    (DailyMetric-based) either never sees them (no metrics on an orphaned
    disk / unattached IP / empty LB) or, for the one zombie type that IS
    metered (stopped_not_deallocated VMs), pass 1's blanket protection
    would otherwise discard the real existence-check result for it.

    For each recheckable category, re-runs that category's own KQL
    condition (from collector_zombie.py) scoped to just the resource IDs
    with an open finding of that type:
      - resource still matches the condition  -> genuinely still a zombie,
        leave Finding open, bump last_checked_at.
      - resource doesn't match (deleted, OR fixed — VM restarted, disk
        reattached, IP assigned, LB got backend members) -> resolve.
      - query fails for a batch -> leave those findings completely
        untouched; a failed check must never be treated as "resolved".

    Returns {zombie_checked, zombie_resolved, zombie_still_open, zombie_check_failed}.
    """
    zombie_findings = list(
        Finding.objects.filter(
            client=client_obj, status="open",
            finding_type__in=ZOMBIE_RECHECK_TYPES,
        ).values("id", "resource_id", "finding_type").distinct()
    )

    result = {"zombie_checked": 0, "zombie_resolved": 0,
              "zombie_still_open": 0, "zombie_check_failed": 0}

    if not zombie_findings:
        return result

    by_category = {}
    for f in zombie_findings:
        by_category.setdefault(f["finding_type"], []).append(f["resource_id"])

    still_zombie_lower = set()
    failed_lower = set()

    for category, resource_ids in by_category.items():
        for batch in _chunks(resource_ids, BATCH_SIZE):
            try:
                query = collector_zombie.recheck_query(category, batch)
                request = QueryRequest(subscriptions=subscription_ids, query=query)
                query_result = rg_client.resources(request)
                for row in query_result.data:
                    still_zombie_lower.add(row["id"].lower())
            except Exception as exc:
                log.warning(f"[{client_obj.slug}] Zombie re-check failed "
                            f"[{category}]: {exc}. Leaving these findings "
                            f"open rather than guessing.")
                failed_lower.update(rid.lower() for rid in batch)

    for f in zombie_findings:
        rid_lower = f["resource_id"].lower()
        result["zombie_checked"] += 1

        if rid_lower in failed_lower:
            result["zombie_check_failed"] += 1
            continue

        if rid_lower in still_zombie_lower:
            result["zombie_still_open"] += 1
            Finding.objects.filter(id=f["id"]).update(last_checked_at=now)
        else:
            result["zombie_resolved"] += 1
            Finding.objects.filter(id=f["id"]).update(
                status="resolved", resolved_at=now, last_checked_at=now,
            )
            log.info(f"  Resolved zombie finding ({f['finding_type']}) — "
                     f"no longer in zombie state: {f['resource_id']}")

    return result


def run_live_check(client_obj: Client, ctx, subscription_ids: list) -> dict:
    """
    Pass 1: checks every distinct resource this client has metric history
    for, UNION'd with every resource_id we have AIResourcePosture data for
    (AI Governance resources don't always have DailyMetric rows — e.g. a
    Cognitive Services account with only Vision/Speech usage produces no
    token metrics), against live Resource Graph data. Updates
    ResourceStatus for all of them — the same source of truth
    findings()/all_resources() already read to hide deleted resources —
    and marks any open Finding 'resolved' if its resource is gone. Skips
    the deletion check for any resource that currently has an open
    Finding of a zombie type NOT covered by pass 2's targeted re-check
    (see ZOMBIE_RECHECK_TYPES) — the generic resources table doesn't
    reliably index those, so we must not falsely mark them deleted here.

    Pass 2: re-verifies open zombie-type findings directly via
    _recheck_zombie_findings — see that function's docstring.

    Returns a summary dict: {checked, deleted, still_exists, skipped,
    zombie_checked, zombie_resolved, zombie_still_open, zombie_check_failed}.
    """
    rg_client = ResourceGraphClient(credential=ctx.credential)
    now = timezone.now()

    daily_metric_ids = (
        DailyMetric.objects.filter(client=client_obj)
        .exclude(resource_id="")
        .values_list("resource_id", flat=True)
        .distinct()
    )
    posture_ids = (
        AIResourcePosture.objects.filter(client=client_obj)
        .values_list("resource_id", flat=True)
        .distinct()
    )
    # Case-insensitive merge — DailyMetric and AIResourcePosture don't
    # normalize resource_id the same way (AIResourcePosture always
    # lower/strip/rstrip's it; DailyMetric doesn't), so a naive set union
    # could otherwise create two ResourceStatus rows for the same physical
    # resource under different casing. DailyMetric's casing wins when both
    # sources have the same resource — arbitrary but stable, and every
    # consumer of ResourceStatus already normalizes to lowercase before
    # comparing anyway.
    _seen_lower = {}
    for rid in daily_metric_ids:
        _seen_lower.setdefault(rid.lower().strip().rstrip("/"), rid)
    for rid in posture_ids:
        _seen_lower.setdefault(rid.lower().strip().rstrip("/"), rid)
    all_resource_ids = list(_seen_lower.values())

    pass1_result = {"checked": 0, "deleted": 0, "still_exists": 0, "skipped": 0}
    pass1_error = None

    if not all_resource_ids:
        log.info(f"[{client_obj.slug}] No metered resources to check.")
    else:
        # Resource IDs that have open zombie-type findings NOT handled by
        # pass 2's targeted re-check — these are protected from being
        # marked deleted because the generic resources table in Resource
        # Graph doesn't reliably index all resource types. The four types
        # in ZOMBIE_RECHECK_TYPES are excluded from this blanket
        # protection since pass 2 gives them a real, targeted check
        # instead.
        ZOMBIE_TYPES = {
            "unattached_public_ip", "orphaned_disk", "unattached_nic",
            "unused_nsg", "aging_snapshot", "empty_load_balancer",
            "stopped_not_deallocated",
        }
        blanket_protected_types = ZOMBIE_TYPES - ZOMBIE_RECHECK_TYPES
        protected_ids_lower = {
            rid.lower()
            for rid in Finding.objects.filter(
                client=client_obj,
                status="open",
                finding_type__in=blanket_protected_types,
            ).values_list("resource_id", flat=True).distinct()
        }

        if protected_ids_lower:
            log.info(f"[{client_obj.slug}] Protecting {len(protected_ids_lower)} "
                     f"zombie resource(s) from false-positive deletion check")

        try:
            existing_ids = _existing_resource_ids(rg_client, subscription_ids, all_resource_ids)
        except Exception as exc:
            log.warning(f"[{client_obj.slug}] Live check query failed: {exc}. "
                        f"Leaving resource status unchanged rather than guessing.")
            pass1_error = str(exc)
            existing_ids = None

        if existing_ids is not None:
            deleted_count = 0
            skipped_count = 0

            for rid in all_resource_ids:
                rid_lower = rid.lower()

                # Never mark a resource deleted if it has an open finding
                # of a blanket-protected zombie type
                if rid_lower in protected_ids_lower:
                    ResourceStatus.objects.update_or_create(
                        client=client_obj,
                        resource_id=rid,
                        defaults={"exists": True},
                    )
                    skipped_count += 1
                    continue

                exists = rid_lower in existing_ids

                ResourceStatus.objects.update_or_create(
                    client=client_obj,
                    resource_id=rid,
                    defaults={"exists": exists},
                )

                if not exists:
                    deleted_count += 1
                    # __iexact, not exact match — rid may come from either
                    # DailyMetric or AIResourcePosture's normalized casing
                    # (see the merge above), and Finding.resource_id isn't
                    # written with either convention consistently.
                    updated = Finding.objects.filter(
                        client=client_obj, resource_id__iexact=rid, status="open",
                    ).exclude(
                        finding_type__in=NON_RESOURCE_FINDING_TYPES,
                    ).update(status="resolved", resolved_at=now, last_checked_at=now)

                    # AI posture/shadow-AI findings are excluded above (see
                    # NON_RESOURCE_FINDING_TYPES), but existence has just
                    # been genuinely reconfirmed here — see
                    # AI_POSTURE_FINDING_TYPES' comment for why that makes
                    # them resolvable now, unlike the other excluded types.
                    updated += Finding.objects.filter(
                        client=client_obj, resource_id__iexact=rid, status="open",
                        finding_type__in=AI_POSTURE_FINDING_TYPES,
                    ).update(status="resolved", resolved_at=now, last_checked_at=now)

                    if updated:
                        log.info(f"  Resolved {updated} finding(s) for deleted resource: {rid}")

            pass1_result = {
                "checked":      len(all_resource_ids),
                "deleted":      deleted_count,
                "still_exists": len(all_resource_ids) - deleted_count,
                "skipped":      skipped_count,
            }

    zombie_result = _recheck_zombie_findings(rg_client, subscription_ids, client_obj, now)

    log.info(
        f"[{client_obj.slug}] Live check: {pass1_result['checked']} resources checked "
        f"(metered + AI-governed), "
        f"{pass1_result['deleted']} deleted, {pass1_result['still_exists']} exist, "
        f"{pass1_result['skipped']} protected · "
        f"{zombie_result['zombie_checked']} zombie findings checked, "
        f"{zombie_result['zombie_resolved']} resolved, "
        f"{zombie_result['zombie_still_open']} still open, "
        f"{zombie_result['zombie_check_failed']} check failed"
    )

    combined = {**pass1_result, **zombie_result}
    if pass1_error:
        combined["error"] = pass1_error
    return combined
