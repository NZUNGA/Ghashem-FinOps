"""
Shared finding-upsert helper used by all analyzers.

update_or_create keyed on (client, resource_id, finding_type) prevents
duplicate open+resolved findings for the same resource — the bug that
occurred when a resource was marked resolved by live_check, then
reappeared (or was re-created) and the pipeline ran again, which would
create a new open finding alongside the preserved resolved one.

Using this helper means:
  - A brand-new finding is created as open.
  - An already-open finding is updated in-place (evidence/savings refresh).
  - A resolved finding that reappears is explicitly reopened — the correct
    behavior when a resource is re-created after being cleaned up.
"""

from django.utils import timezone
from core.models import Finding


def upsert_finding(
    *,
    client,
    run_obj,
    resource_id,
    resource_name,
    resource_group,
    finding_type,
    analyzer,
    severity,
    monthly_savings,
    evidence,
    action,
    extra_data=None,
):
    """
    Create or update a Finding, keyed on (client, resource_id, finding_type).

    Returns (instance, created) matching Django's update_or_create convention.
    """
    # Normalized once, here, for every analyzer that calls this helper —
    # some analyzers (zombie, resource_utilization, idle, token) pass
    # resource_id straight from Resource Graph / DailyMetric with
    # whatever casing Azure returned, uncontrolled by this helper until
    # now. Without this, the same physical resource can end up as two
    # different Finding rows (e.g. ".../VM-A" vs ".../vm-a") because the
    # (client, resource_id, finding_type) uniqueness constraint is
    # case-sensitive at the DB level.
    resource_id = resource_id.lower().strip().rstrip("/")

    obj, created = Finding.objects.update_or_create(
        client=client,
        resource_id=resource_id,
        finding_type=finding_type,
        defaults=dict(
            pipeline_run=run_obj,
            resource_name=resource_name,
            resource_group=resource_group or "",
            analyzer=analyzer,
            severity=severity,
            estimated_monthly_savings_usd=round(monthly_savings, 2),
            evidence=evidence,
            action=action,
            extra_data=extra_data or {},
            status="open",
            resolved_at=None,
        ),
    )
    return obj, created
