import logging
import re
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseNotAllowed
from django.db.models import Sum, Count, Avg, Q, Subquery, OuterRef, Min, Max
from django.db.models.functions import TruncMonth, Lower
from django.db import connection as db_conn
from django.utils import timezone
from core.models import Client, Finding, PipelineRun, DailyMetric, DailyCost, UserClientAccess, ResourceStatus, AccessLog, DailyAIUsage, AIResourcePosture
from core.config import load_config
from core.access import require_client_access, require_client_admin, staff_member_required, require_tier
from core.utils import PCT_DISPLAY_CAP

log = logging.getLogger(__name__)

# Finding types that belong on the AI Governance page: security/governance
# risk rather than cost impact, $0 estimated savings by design, and full
# context (posture score, remediation) on /ai-governance/. Counted for the
# Overview callout and kept out of its savings sum — Overview resource cards
# are hidden by cost/savings, not by this list (see summary()).
AI_GOVERNANCE_FINDING_TYPES = {
    "ai_local_auth_enabled",
    "ai_public_network_access",
    "ai_no_content_filter",
    "ai_no_private_endpoint",
    "shadow_ai_resource",
    "idle_ai_deployment",
    "ai_cost_spike",
    "untagged_ai_resource",
}


def _get_client(slug):
    return get_object_or_404(Client, slug=slug)


# ── View cache helpers ───────────────────────────────────────────────────────
# DatabaseCache (see ghashem/settings.py CACHES) requires `python manage.py
# createcachetable` to have run — deploy ordering can't guarantee that's
# happened before this code goes live, so every cache access is wrapped
# defensively: a missing/broken cache table degrades to running the normal
# queries instead of 500ing the page, same pattern as this codebase's other
# non-critical-path guards (email, Advisor, live-check).
def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _cache_set(key, value, timeout=300):
    try:
        cache.set(key, value, timeout)
    except Exception:
        pass


def _cache_delete(key):
    try:
        cache.delete(key)
    except Exception:
        pass


def _authorized_clients(request):
    """
    Returns only the clients this user is allowed to see — used to
    populate the sidebar client selector so it never reveals the
    existence of clients the user has no access to.
    """
    if request.user.is_superuser:
        return Client.objects.filter(active=True)
    accessible_ids = UserClientAccess.objects.filter(
        user=request.user
    ).values_list("client_id", flat=True)
    return Client.objects.filter(active=True, id__in=accessible_ids)


def _client_list():
    try:
        cfg = load_config()
        return cfg.active_clients()
    except Exception:
        return []


def _window_context(client):
    """
    Derives a monthly-average factor and display label from the client's
    actual analysis window, instead of assuming a fixed 6-month span —
    core/config.py defaults analysis_start/analysis_end to a rolling
    90-day window, not 6 months, and either can be overridden per client.
    """
    window_days = (client.analysis_end - client.analysis_start).days or 1
    monthly_factor = 30 / window_days
    return window_days, monthly_factor, f"{window_days}-day"


# ── SSO auth completion (intercepts before social_django) ───────────────────

def auth_complete(request, backend):
    """
    Intercepts /auth/complete/<backend>/ before social_django processes
    it (see ghashem/urls.py — this must stay registered ahead of the
    social_django.urls include, same as the view it replaces).

    Handles two cases:
    1. Admin consent callback from Microsoft: Microsoft's tenant-wide
       admin-consent endpoint (distinct from the normal OAuth2
       authorize/token flow) redirects back here with `admin_consent=True`
       and no `state` param — social_django's complete view expects a
       normal OAuth2 callback and raises AuthMissingParameter on this,
       producing a 500. This is NOT a login, just Microsoft confirming
       tenant consent was granted; the admin still logs in separately.
    2. Normal OAuth2 login callback — passed through to social_django's
       real complete view unchanged.
    """
    from social_django.views import complete as social_complete

    if request.GET.get("admin_consent") == "True":
        from django.contrib import messages
        messages.success(
            request,
            "Access granted successfully. Your organization can now "
            "sign in to Ghashem.",
        )
        return redirect("dashboard:home")

    return social_complete(request, backend)


# ── Home ──────────────────────────────────────────────────────────────────────

@login_required
def home(request):
    clients = _authorized_clients(request)
    if clients.count() == 1:
        return redirect("dashboard:summary", slug=clients.first().slug)
    return render(request, "dashboard/home.html", {"clients": clients})


# ── Health check (unauthenticated — for uptime monitors / load balancers) ───

def health(request):
    try:
        db_conn.ensure_connection()
        db_ok = True
    except Exception:
        db_ok = False

    return JsonResponse(
        {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "error",
        },
        status=200 if db_ok else 503,
    )


# ── Executive summary ─────────────────────────────────────────────────────────

@login_required
@require_client_access
def summary(request, slug):
    client = request.client
    # Authorized-clients list is scoped to THIS user's grants (superusers
    # see every active client; everyone else only what UserClientAccess
    # grants them) — it must never be cached under the client-scoped cache
    # key below, or one user's sidebar could leak another, differently-
    # privileged user's view of which clients exist. Always fetched fresh.
    clients = _authorized_clients(request)

    cache_key = f"summary_{client.id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        cached["clients"] = clients
        return render(request, "dashboard/summary.html", cached)

    # Active findings drive every stat on this page — a resolved finding
    # (resource no longer exists) shouldn't count toward "money you're
    # currently losing", even though it's still useful as a track record
    # elsewhere on the dashboard.
    # Resources live_check.py has confirmed deleted — excluded from every
    # figure on this page so a deleted resource can't inflate savings or
    # appear in the resource table in the window before its findings are
    # resolved. Normalized the same way upsert_finding() normalizes
    # Finding.resource_id (lower/strip/rstrip "/"), since ResourceStatus
    # keeps whatever casing DailyMetric had and a raw comparison would
    # silently miss mixed-case rows (same pattern as ai_governance_page()).
    deleted_ids_lower = {
        rid.lower().strip().rstrip("/")
        for rid in ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }
    all_open_findings = Finding.objects.filter(
        client=client, status="open"
    ).exclude(resource_id__in=deleted_ids_lower)
    resolved_findings = Finding.objects.filter(client=client, status="resolved")
    last_run = PipelineRun.objects.filter(client=client).order_by("-started_at").first()

    # Spend lookups needed up front: a resource with NO billing data in the
    # window AND $0 total estimated savings across all its open findings is
    # hidden from the Overview (see hidden_resource_ids below).
    window_start = client.analysis_start or (date.today() - timedelta(days=90))
    monthly_cost_by_resource = _monthly_cost_by_resource(client, window_start)
    window_days, monthly_factor, window_label = _window_context(client)

    # Hide resource cards that carry neither cost nor savings — typically an
    # AI account with only posture/governance findings ($0 savings by
    # design, see analyzer_ai_posture.py) and no billing rows, which reads
    # as a broken HIGH card with three dashes in a cost-savings table. Those
    # findings still live on /ai-governance/. A resource with the same
    # findings but real billing data stays visible (the cost shows that it's
    # active and worth reviewing), and a mixed resource stays visible
    # because its cost-type finding carries the savings. The synthetic
    # cost_anomaly resource is never hidden. Excluded by the exact stored
    # resource_id values so it can't miss on casing.
    hidden_resource_ids = [
        row["resource_id"]
        for row in (
            all_open_findings.exclude(finding_type="cost_anomaly")
            .values("resource_id")
            .annotate(s=Sum("estimated_monthly_savings_usd"))
        )
        if not (row["s"] or 0)
        and not round(
            monthly_cost_by_resource.get(
                row["resource_id"].lower().strip().rstrip("/"), 0) or 0, 2)
    ]
    visible_findings = all_open_findings.exclude(resource_id__in=hidden_resource_ids)

    # Cost anomaly is client-wide (not a per-resource saving) — it's
    # surfaced only as the spike banner above the table, so it's kept out
    # of the resource cards, the open-findings count/breakdown and the
    # charts (they'd otherwise disagree with the visible table, and its
    # "savings" figure is a projected spike, not something saveable). It
    # still counts toward severity_counts below, which drives the sidebar
    # urgency indicator, and still appears on the full Findings page.
    findings = visible_findings.exclude(finding_type="cost_anomaly")

    cost_anomaly = visible_findings.filter(finding_type="cost_anomaly").first()
    anomaly_pct = None
    anomaly_pct_capped = False
    if cost_anomaly:
        raw_pct = cost_anomaly.extra_data.get("pct_over_baseline", 0)
        anomaly_pct_capped = raw_pct > PCT_DISPLAY_CAP
        anomaly_pct = PCT_DISPLAY_CAP if anomaly_pct_capped else raw_pct

    # cost_anomaly's estimated_monthly_savings_usd is the projected monthly
    # spike (extra spend), not something the client can save — it stays in
    # the findings list/banner but must not count toward "savings".
    # AI governance types are excluded explicitly too — they carry $0 by
    # design, so this changes nothing numerically, but it keeps the savings
    # sum honest if one of them ever gets a non-zero estimate.
    # (cost_anomaly is already out of `findings` above, so it can't reach
    # this sum.)
    savings_findings = findings.exclude(
        finding_type__in=AI_GOVERNANCE_FINDING_TYPES
    )
    # total_savings / annual_savings are computed below, once
    # monthly_cost_by_resource exists — per-resource savings are capped at
    # that resource's real monthly cost.

    # severity_counts feeds base.html's sidebar (health dot, "N urgent
    # findings", Findings nav badge), so it includes the cost anomaly —
    # a high-severity spike must still read as urgent there.
    # table_severity_counts matches what the Overview table/charts show.
    severity_counts = {
        "high":   visible_findings.filter(severity="high").count(),
        "medium": visible_findings.filter(severity="medium").count(),
        "low":    visible_findings.filter(severity="low").count(),
    }
    table_severity_counts = {
        "high":   findings.filter(severity="high").count(),
        "medium": findings.filter(severity="medium").count(),
        "low":    findings.filter(severity="low").count(),
    }

    finding_type_data = list(
        findings.values("finding_type")
        .annotate(count=Count("id"), savings=Sum("estimated_monthly_savings_usd"))
        .order_by("-savings")
    )
    for t in finding_type_data:
        t["label"] = FINDING_LABELS.get(t["finding_type"], t["finding_type"])

    # Windowed, not all-time — total_spend must not grow indefinitely as
    # historical DailyCost rows accumulate across pipeline runs
    # (window_start is defined above, next to monthly_cost_by_resource).
    spend_agg = _live_costs(client, date__gte=window_start).aggregate(
        s=Sum("billed_cost"), first=Min("date"), last=Max("date"))
    total_spend = spend_agg["s"] or 0

    spend_by_category = (
        _live_costs(client, date__gte=window_start)
        .exclude(service_category="")
        .values("service_category")
        .annotate(total=Sum("billed_cost"))
        .order_by("-total")
    )
    # View-side percentage calculation — Django's template language has no
    # built-in div/mul filters to do this in summary.html directly.
    spend_by_category_with_pct = [
        {
            "service_category": r["service_category"],
            "total": r["total"],
            "pct": round(float(r["total"]) / float(total_spend) * 100, 0)
                   if total_spend else 0,
        }
        for r in spend_by_category
    ]

    # total_spend covers however much of the window has billing while
    # savings is per month — convert using the client's actual billed span
    # (first→last billed date, minimum 7 days) rather than the nominal
    # window, so a client with under 90 days of billing isn't understated
    # and the headline agrees with the per-resource cards.
    monthly_spend = round(
        _monthly_from_span(total_spend, spend_agg["first"], spend_agg["last"]), 2
    )

    total_savings = _capped_total_savings(
        savings_findings, monthly_cost_by_resource
    )
    annual_savings = total_savings * 12

    # Grouped by resource (not finding type/category) — sorted by real
    # monthly spend descending, each resource's findings as bullet points
    # underneath. See _build_resource_findings() docstring.
    resource_findings = _build_resource_findings(
        findings.order_by("-estimated_monthly_savings_usd"),
        monthly_cost_by_resource,
    )

    # AI usage summary (last 30 days) — AI Cost Intelligence module.
    ai_window_start = date.today() - timedelta(days=30)

    ai_summary = DailyAIUsage.objects.filter(
        client=client, date__gte=ai_window_start
    ).aggregate(
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_tokens=Sum("total_tokens"),
        total_cost=Sum("estimated_cost_usd"),
    )
    # django.contrib.humanize (|intcomma) isn't installed — pre-format
    # the comma separator here instead.
    ai_summary["total_tokens_display"] = f"{ai_summary['total_tokens'] or 0:,}"

    ai_by_deployment = list(
        DailyAIUsage.objects
        .filter(client=client, date__gte=ai_window_start)
        .values("resource_name", "deployment_name", "model_name", "resource_id")
        .annotate(
            tokens=Sum("total_tokens"),
            input_tok=Sum("input_tokens"),
            output_tok=Sum("output_tokens"),
            cost=Sum("estimated_cost_usd"),
        )
        .order_by("-tokens")
    )

    # Count of AI governance/posture findings for the Overview callout that
    # points to /ai-governance/ — they're kept out of the savings table.
    ai_finding_count = all_open_findings.filter(
        finding_type__in=AI_GOVERNANCE_FINDING_TYPES
    ).count()

    # From all_open_findings, not the card-filtered `findings`, so hiding a
    # zero-cost/zero-savings resource card doesn't also empty this tab.
    ai_findings = all_open_findings.filter(
        finding_type__in=[
            "idle_ai_deployment",
            "ai_cost_spike",
            "untagged_ai_resource",
        ]
    )

    # AIResourcePosture — one row per (client, resource_id, pipeline_run)
    # by design (see core.models.AIResourcePosture docstring), so
    # posture history survives across runs instead of being overwritten
    # in place. That means a naive .filter(client=client) here would
    # return every historical scan, not just the current state — this
    # subquery keeps only the most recently scanned row per resource_id.
    latest_scan = AIResourcePosture.objects.filter(
        client=client,
        resource_id=OuterRef("resource_id"),
    ).order_by("-scanned_at").values("id")[:1]

    ai_postures = AIResourcePosture.objects.filter(
        client=client,
        id__in=Subquery(latest_scan),
    ).order_by("-posture_score")

    context = {
        "client":          client,
        "active_page":     "summary",
        "last_run":        last_run,
        "total_savings":   total_savings,
        "annual_savings":  annual_savings,
        "severity_counts": severity_counts,
        "table_severity_counts": table_severity_counts,
        "finding_types":   finding_type_data,
        "resource_findings": resource_findings,
        "window_days":     window_days,
        "window_label":    window_label,
        "total_findings":  findings.count(),
        "resolved_count":  resolved_findings.count(),
        "total_spend":     total_spend,
        "monthly_spend":   monthly_spend,
        "cost_anomaly":        cost_anomaly,
        "anomaly_pct":         anomaly_pct,
        "anomaly_pct_capped":  anomaly_pct_capped,
        "spend_by_category_with_pct": spend_by_category_with_pct,
        "ai_summary":       ai_summary,
        "ai_by_deployment": ai_by_deployment,
        "ai_findings":      list(ai_findings),
        "ai_finding_count": ai_finding_count,
        "ai_window_days":   30,
        "ai_postures":      list(ai_postures),
    }
    # "clients" is deliberately excluded — see the fetch-fresh comment above.
    _cache_set(cache_key, dict(context))
    context["clients"] = clients
    return render(request, "dashboard/summary.html", context)


# ── AI Governance (dedicated page) ───────────────────────────────────────────

@login_required
@require_client_access
def ai_governance_page(request, slug):
    """
    Dedicated AI Governance page — posture score, per-resource cards,
    governance findings, shadow AI, and token cost breakdown across
    every Cognitive Services resource the client has (any kind, per
    the Session 5b coverage widening), not just OpenAI/AIServices.
    """
    client = request.client
    # See summary()'s comment — this is scoped to the requesting user's
    # own grants and must never be cached under the client-scoped key below.
    clients = _authorized_clients(request)

    cache_key = f"ai_governance_{client.id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        cached["clients"] = clients
        return render(request, "dashboard/ai_governance.html", cached)

    last_run = PipelineRun.objects.filter(client=client).order_by("-started_at").first()

    # Resource IDs confirmed deleted by live_check.py — same ResourceStatus
    # source of truth findings()/all_resources() already read. A deleted
    # resource's history (AIResourcePosture rows, resolved findings) is
    # preserved, not shown here — see dashboard:resource_history.
    deleted_ids_lower = {
        rid.lower().strip().rstrip("/")
        for rid in ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }

    # ── Posture (latest scan per resource — AIResourcePosture keeps one
    # row per pipeline run for history, see core.models docstring) ──
    latest_posture_ids = (
        AIResourcePosture.objects
        .filter(client=client, resource_id=OuterRef("resource_id"))
        .order_by("-scanned_at")
        .values("id")[:1]
    )
    postures = (
        AIResourcePosture.objects
        .filter(client=client, id__in=Subquery(latest_posture_ids))
        .exclude(resource_id__in=deleted_ids_lower)
        .order_by("posture_score")
    )

    avg_score = postures.aggregate(avg=Avg("posture_score"))["avg"]
    overall_posture_score = round(avg_score, 1) if avg_score is not None else None
    # Precomputed rather than done in-template (score|floatformat:0
    # followed by a literal "0" loses precision — 7.3 would render as
    # "70%" instead of 73%).
    overall_posture_pct = round(avg_score * 10) if avg_score is not None else 0

    # No icon font is loaded anywhere in this codebase (confirmed —
    # base.html only loads Google Fonts + Chart.js) — every "icon"
    # elsewhere in the app is a plain Unicode/emoji character directly
    # in HTML (⚙ ◈ ⚑ ⬡ ◎ 📊 📄 📅), not an icon-font class. Same
    # pattern here, not `ti-*` classes.
    control_summary = {}
    for field, label, icon in [
        ("local_auth_disabled",      "Local auth disabled",       "🔑"),
        ("public_network_disabled",  "Public network disabled",   "🌐"),
        ("private_endpoint_present", "Private endpoint present",  "🔒"),
        ("content_filter_enabled",   "Content filter enabled",    "🧱"),
        ("required_tags_present",    "Governance tags present",   "🏷"),
        ("defender_enabled",         "Defender for AI enabled",   "🛡"),
    ]:
        control_summary[field] = {
            "label": label,
            "icon": icon,
            "pass":    postures.filter(**{field: True}).count(),
            "fail":    postures.filter(**{field: False}).count(),
            "unknown": postures.filter(**{f"{field}__isnull": True}).count(),
        }

    # ── AI cost & usage (last 30 days) — same shape as summary()'s AI
    # Governance tab, intentionally kept in sync with it ──
    ai_window_start = date.today() - timedelta(days=30)

    ai_summary = DailyAIUsage.objects.filter(
        client=client, date__gte=ai_window_start
    ).aggregate(
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_tokens=Sum("total_tokens"),
        total_cost=Sum("estimated_cost_usd"),
    )
    ai_summary["total_tokens_display"] = f"{ai_summary['total_tokens'] or 0:,}"

    ai_by_deployment = list(
        DailyAIUsage.objects
        .filter(client=client, date__gte=ai_window_start)
        .values("resource_name", "deployment_name", "model_name", "resource_id")
        .annotate(
            tokens=Sum("total_tokens"),
            input_tok=Sum("input_tokens"),
            output_tok=Sum("output_tokens"),
            cost=Sum("estimated_cost_usd"),
        )
        .order_by("-tokens")
    )

    # ── Governance findings ──
    GOVERNANCE_FINDING_TYPES = [
        "ai_local_auth_enabled",
        "ai_public_network_access",
        "ai_no_content_filter",
        "ai_no_private_endpoint",
        "shadow_ai_resource",
        "untagged_ai_resource",
        "idle_ai_deployment",
        "ai_cost_spike",
    ]
    # Belt-and-suspenders against the deleted_ids_lower exclusion above:
    # live_check.py resolves these findings on the next run after a
    # resource is deleted, but a finding could still be briefly "open" in
    # the window between deletion and the next live_check — this exclusion
    # ensures the findings table can never show a deleted resource either.
    governance_findings = Finding.objects.filter(
        client=client, finding_type__in=GOVERNANCE_FINDING_TYPES, status="open",
    ).exclude(resource_id__in=deleted_ids_lower)

    SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    governance_findings_sorted = sorted(
        governance_findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.resource_name),
    )

    shadow_findings = [f for f in governance_findings_sorted if f.finding_type == "shadow_ai_resource"]
    posture_findings = [f for f in governance_findings_sorted if f.finding_type != "shadow_ai_resource"]
    shadow_resource_ids = {f.resource_id.lower() for f in shadow_findings}

    # ── Resource cards — posture joined with usage. Both resource_id
    # sets are normalized with the same lower/strip/rstrip("/") used
    # everywhere else in this codebase (Finding.resource_id and
    # AIResourcePosture.resource_id already are; DailyAIUsage.resource_id
    # is currently only .lower()'d at write time in collector_ai.py, not
    # part of this task's scope to change — normalizing again here on
    # read is a no-op for already-clean ids and a safety net otherwise).
    def _norm(rid):
        return rid.lower().strip().rstrip("/")

    posture_by_resource = {_norm(p.resource_id): p for p in postures}
    usage_by_resource = {_norm(d["resource_id"]): d for d in ai_by_deployment}
    all_resource_ids = set(posture_by_resource.keys()) | set(usage_by_resource.keys())

    resource_cards = []
    for rid in all_resource_ids:
        posture = posture_by_resource.get(rid)
        usage = usage_by_resource.get(rid)
        tokens = (usage["tokens"] if usage else 0) or 0
        resource_cards.append({
            "resource_id": rid,
            "resource_name": posture.resource_name if posture else usage["resource_name"],
            "resource_group": posture.resource_group if posture else "",
            "resource_type": posture.resource_type if posture else "unknown",
            "posture_score": posture.posture_score if posture else None,
            "local_auth_disabled": posture.local_auth_disabled if posture else None,
            "public_network_disabled": posture.public_network_disabled if posture else None,
            "private_endpoint_present": posture.private_endpoint_present if posture else None,
            "content_filter_enabled": posture.content_filter_enabled if posture else None,
            "required_tags_present": posture.required_tags_present if posture else None,
            "total_tokens": tokens,
            "total_tokens_display": f"{tokens:,}",
            "estimated_cost": float(usage["cost"]) if usage and usage["cost"] else 0,
            "is_idle": tokens == 0,
            "is_shadow": rid in shadow_resource_ids,
        })

    # Highest risk first — lowest posture score, then name. (Resources
    # with no posture row at all — usage-only, shouldn't happen given
    # collector_ai.py now discovers every kind, but handled defensively —
    # sort after every scored resource, not before.)
    resource_cards.sort(
        key=lambda r: (r["posture_score"] if r["posture_score"] is not None else 999, r["resource_name"])
    )

    compliance_summary = {
        "total_resources": len(resource_cards),
        "passing_local_auth": sum(1 for r in resource_cards if r["local_auth_disabled"] is True),
        "passing_public_net": sum(1 for r in resource_cards if r["public_network_disabled"] is True),
        "passing_tags": sum(1 for r in resource_cards if r["required_tags_present"] is True),
        "shadow_count": len(shadow_findings),
        "high_findings": sum(1 for f in posture_findings if f.severity == "high"),
    }

    context = {
        "client":  client,
        "active_page": "ai_governance",
        "last_run": last_run,
        "overall_posture_score": overall_posture_score,
        "overall_posture_pct":   overall_posture_pct,
        "control_summary": control_summary,
        "resource_cards": resource_cards,
        "ai_summary": ai_summary,
        "ai_by_deployment": ai_by_deployment,
        "posture_findings": posture_findings,
        "shadow_findings": shadow_findings,
        "compliance_summary": compliance_summary,
        "window_days": 30,
        "finding_labels": FINDING_LABELS,
    }
    # "clients" is deliberately excluded — see the fetch-fresh comment above.
    _cache_set(cache_key, dict(context))
    context["clients"] = clients
    return render(request, "dashboard/ai_governance.html", context)


# ── Findings ──────────────────────────────────────────────────────────────────

FINDING_LABELS = {
    "token_inefficiency": "Token inefficiency",
    "idle_account":       "Idle / underutilized",
    "wrong_tier":         "Wrong tier / right-sizing",
    "rate_limiting":      "Rate limiting",
    "duplicate_accounts": "Duplicate accounts",
    "stale_model":        "Stale custom model",
    "unattached_public_ip":     "Unattached public IP",
    "orphaned_disk":            "Orphaned managed disk",
    "unattached_nic":           "Unattached network interface",
    "unused_nsg":               "Unused security group",
    "aging_snapshot":           "Aging snapshot",
    "stopped_not_deallocated":  "Stopped, not deallocated",
    "empty_load_balancer":      "Empty load balancer",
    "no_autoshutdown":          "No auto-shutdown schedule",
    "cost_anomaly":             "Cost anomaly",
    "reservation_opportunity":  "Reserved Instance opportunity",
    "idle_ai_deployment":       "Idle AI model deployment",
    "ai_cost_spike":            "AI token usage spike",
    "untagged_ai_resource":     "AI resource missing tags",
    "ai_local_auth_enabled":    "API key auth enabled on AI resource",
    "ai_public_network_access": "AI resource on public internet",
    "ai_no_content_filter":     "No content filter on AI deployment",
    "ai_no_private_endpoint":   "AI resource without private endpoint",
    "shadow_ai_resource":       "Ungoverned AI resource",
}

# Plain-English resource-type buckets for the overview page — the buyer is
# a non-technical finance/IT manager, so findings are grouped by what the
# resource IS, not by the raw finding_type slug or Azure namespace string.
FINDING_CATEGORY = {
    "stopped_not_deallocated":  "Virtual Machines",
    "no_autoshutdown":          "Virtual Machines",
    "orphaned_disk":            "Disks",
    "aging_snapshot":           "Disks",
    "unattached_public_ip":     "Public IPs",
    "empty_load_balancer":      "Load Balancers",
    "unattached_nic":           "Network",
    "unused_nsg":               "Network",
    "token_inefficiency":       "AI / Cognitive Services",
    "idle_account":             "Underutilized Resource",
    "wrong_tier":               "Overprovisioned Resource",
    "rate_limiting":            "AI / Cognitive Services",
    "duplicate_accounts":       "AI / Cognitive Services",
    "stale_model":              "AI / Cognitive Services",
    "cost_anomaly":             "Cost Anomalies",
}

# Finding types sourced from Resource Graph — these resources have no
# DailyMetric rows since there's nothing to measure on an orphaned disk
# or unattached IP. Used to:
#   1. Augment the Resources page with zombie resources from Findings.
#   2. Protect these resources in live_check from false-positive deletion
#      since the generic `resources` table doesn't reliably index all types.
ZOMBIE_FINDING_TYPES = {
    "unattached_public_ip",
    "orphaned_disk",
    "unattached_nic",
    "unused_nsg",
    "aging_snapshot",
    "empty_load_balancer",
    "stopped_not_deallocated",
}

# Friendly labels for resource namespaces, used in the deep-dive dropdown
NAMESPACE_LABELS = {
    "Microsoft.CognitiveServices/accounts": "Cognitive Services",
    "Microsoft.Compute/virtualMachines":    "Virtual machines",
    "Microsoft.Storage/storageAccounts":    "Storage accounts",
    "Microsoft.Web/sites":                  "Web / App Services",
}

# Which metrics matter for each namespace — drives which charts render
# on the deep-dive page for a given resource
NAMESPACE_METRICS = {
    "Microsoft.CognitiveServices/accounts": [
        "TotalCalls", "BlockedCalls", "ProcessedPromptTokens", "GeneratedTokens",
    ],
    "Microsoft.Compute/virtualMachines": [
        "Percentage CPU", "Available Memory Bytes",
        "Network In Total", "Network Out Total",
    ],
    "Microsoft.Storage/storageAccounts": [
        "UsedCapacity", "Transactions",
    ],
    "Microsoft.Web/sites": [
        "CpuTime", "Requests", "MemoryWorkingSet",
    ],
}


def _cap_savings(raw_savings, monthly_cost):
    """
    You can't save more than a resource costs. Several findings on one
    resource (e.g. no_autoshutdown 65% + wrong_tier 35% + a stopped VM at
    100%) are each computed against the same underlying cost, so their
    plain sum can exceed it. With no billing data at all (monthly_cost 0 —
    e.g. a list-price-fallback public IP) there's nothing to cap against,
    so the estimate is kept as-is.
    """
    if monthly_cost > 0:
        return min(raw_savings, monthly_cost)
    return raw_savings


def _capped_total_savings(savings_qs, monthly_cost_by_resource):
    """
    Sum of per-resource savings, each capped at that resource's monthly
    cost. One grouped query for the findings plus the already-computed
    monthly_cost_by_resource lookup (lowercased ids), so no per-resource
    query loop.
    """
    total = 0.0
    for row in (savings_qs.values("resource_id")
                .annotate(s=Sum("estimated_monthly_savings_usd"))):
        rid = row["resource_id"].lower().strip().rstrip("/")
        monthly_cost = monthly_cost_by_resource.get(rid, 0) or 0
        total += _cap_savings(row["s"] or 0.0, monthly_cost)
    return round(total, 2)


def _build_resource_findings(findings_qs, monthly_cost_by_resource):
    """
    Groups a Finding queryset by resource_id into resource-level cards —
    each card shows the resource's real monthly Azure spend (from
    monthly_cost_by_resource, a {resource_id.lower(): monthly cost} dict
    built by _monthly_cost_by_resource() — see summary()/findings())
    alongside the combined savings across
    every finding filed against that resource, with per-finding detail as
    bullet points underneath. Shared by summary.html's overview card and
    findings.html's "open" tab so the two can't drift out of sync with
    each other.

    Sorted by highest severity first (high, then medium, then low), and
    by real monthly spend descending within the same severity tier —
    resources with no billing data (monthly_cost_real == 0) naturally
    sort to the bottom of their severity tier.
    """
    resource_map = defaultdict(list)
    for f in findings_qs:
        rid = f.resource_id.lower().strip().rstrip("/")
        resource_map[rid].append(f)

    resource_findings = []
    for rid, flist in resource_map.items():
        monthly_cost_real = round(monthly_cost_by_resource.get(rid, 0) or 0, 2)

        total_savings_mo = _cap_savings(
            sum(float(f.estimated_monthly_savings_usd or 0) for f in flist),
            monthly_cost_real,
        )
        total_savings_yr = round(total_savings_mo * 12, 2)
        total_savings_mo = round(total_savings_mo, 2)

        first = flist[0]
        resource_findings.append({
            "resource_id": rid,
            "resource_name": first.resource_name or rid.split("/")[-1],
            "resource_group": first.resource_group or "",
            "monthly_cost_real": monthly_cost_real,
            "total_savings_mo": total_savings_mo,
            "total_savings_yr": total_savings_yr,
            "highest_severity": (
                "high" if any(f.severity == "high" for f in flist)
                else "medium" if any(f.severity == "medium" for f in flist)
                else "low"
            ),
            "findings": [
                {
                    "finding_type": f.finding_type,
                    "label": FINDING_LABELS.get(
                        f.finding_type, f.get_finding_type_display()
                    ),
                    "severity": f.severity,
                    "action": f.action,
                    "evidence": f.evidence,
                    "monthly_savings": float(f.estimated_monthly_savings_usd or 0),
                    "annual_savings": float(f.estimated_monthly_savings_usd or 0) * 12,
                    "id": f.id,
                }
                for f in sorted(
                    flist,
                    key=lambda x: (
                        0 if x.severity == "high"
                        else 1 if x.severity == "medium"
                        else 2
                    ),
                )
            ],
        })

    SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    resource_findings.sort(
        key=lambda x: (
            SEVERITY_ORDER.get(x["highest_severity"], 3),
            -x["monthly_cost_real"],
        )
    )
    return resource_findings


def _deleted_resource_ids(client):
    """Resource ids live_check.py confirmed deleted, normalized like every
    other deleted-id set in this file (lower/strip/rstrip "/"). A resource
    with no ResourceStatus row (never checked) counts as existing."""
    return {
        rid.lower().strip().rstrip("/")
        for rid in ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }


def _live_costs(client, **filters):
    """
    DailyCost rows for the client, minus rows belonging to resources
    confirmed deleted — their historical billing is real money but not
    current spend, and inflates spend totals/run-rates. Matched on the
    lowercased id in SQL so legacy mixed-case rows fold in too.
    """
    qs = DailyCost.objects.filter(client=client, **filters)
    deleted = _deleted_resource_ids(client)
    if deleted:
        qs = qs.annotate(rid_lc=Lower("resource_id")).exclude(rid_lc__in=deleted)
    return qs


MIN_BILLING_SPAN_DAYS = 7


def _monthly_from_span(total, first_date, last_date):
    """
    Monthly run-rate from a billing total and the first/last dates it was
    billed: total × 30 / span, where span is the actual billed days
    (last − first + 1), never less than MIN_BILLING_SPAN_DAYS so a resource
    with one or two days of billing isn't extrapolated 15-30×. Dividing by
    the nominal 90-day window instead understated any resource (or client)
    with less than a full window of billing.
    """
    if not total or not first_date or not last_date:
        return 0.0
    span = max((last_date - first_date).days + 1, MIN_BILLING_SPAN_DAYS)
    return total * 30.0 / span


def _monthly_cost_by_resource(client, window_start):
    """
    {resource_id.lower(): monthly cost} using each resource's actual billed
    span — shared by summary() and findings() so the card cost, the savings
    cap and the hide-zero-cost rule can't drift apart. Grouped on the
    lowercased id in SQL so legacy mixed-case DailyCost rows fold into the
    same resource.
    """
    monthly = {}
    for r in (_live_costs(client, date__gte=window_start)
              .exclude(resource_id="")
              .annotate(rid=Lower("resource_id"))
              .values("rid")
              .annotate(total=Sum("billed_cost"),
                        first=Min("date"), last=Max("date"))):
        monthly[r["rid"]] = _monthly_from_span(r["total"], r["first"], r["last"])
    return monthly


@login_required
@require_client_access
def findings(request, slug):
    client  = request.client
    clients = _authorized_clients(request)

    from django.utils import timezone
    from datetime import timedelta

    status      = request.GET.get("status", "open")
    severity    = request.GET.get("severity", "")
    ftype       = request.GET.get("type", "")
    min_savings = request.GET.get("min_savings", "0")

    # Resource IDs confirmed deleted — excluded from open/stale/all views.
    # Resources with no ResourceStatus row are assumed to still exist.
    deleted_ids_lower = {
        rid.lower()
        for rid in ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }

    # Resource IDs deleted within the last 90 days — shown in the
    # "Recently removed" history tab only.
    cutoff_90 = timezone.now() - timedelta(days=90)
    recently_deleted_ids_lower = {
        rid.lower()
        for rid in ResourceStatus.objects.filter(
            client=client,
            exists=False,
            last_checked_at__gte=cutoff_90,
        ).values_list("resource_id", flat=True)
    }

    if status == "removed":
        # History tab — findings for resources deleted in the last 90 days.
        # Shows open and resolved together so the client can see what was
        # fixed vs what was simply abandoned/deleted.
        if recently_deleted_ids_lower:
            qs = Finding.objects.filter(
                client=client,
                resource_id__in=recently_deleted_ids_lower,
            ).order_by("-estimated_monthly_savings_usd")
        else:
            qs = Finding.objects.none()
    else:
        # Normal tabs — never show findings for deleted resources.
        qs = Finding.objects.filter(client=client).exclude(
            resource_id__in=deleted_ids_lower
        )
        if status != "all":
            qs = qs.filter(status=status)
        if severity:
            qs = qs.filter(severity=severity)
        if ftype:
            qs = qs.filter(finding_type=ftype)
        try:
            qs = qs.filter(estimated_monthly_savings_usd__gte=float(min_savings))
        except ValueError:
            pass
        qs = qs.order_by("-estimated_monthly_savings_usd")

    # Resource-grouped card view — only meaningful for the "open" tab
    # (matches summary()'s findings.filter(status="open") semantics).
    # Resolved/all/removed keep their existing per-finding-card layout,
    # since those statuses need muted/struck-through/history rendering
    # that a resource-level savings rollup doesn't represent.
    resource_findings = []
    if status == "open":
        window_start = client.analysis_start or (date.today() - timedelta(days=90))
        monthly_cost_by_resource = _monthly_cost_by_resource(client, window_start)
        resource_findings = _build_resource_findings(qs, monthly_cost_by_resource)

    available_types = (
        Finding.objects.filter(client=client)
        .exclude(resource_id__in=deleted_ids_lower)
        .values_list("finding_type", flat=True)
        .distinct()
    )

    base_qs = Finding.objects.filter(client=client).exclude(
        resource_id__in=deleted_ids_lower
    )
    open_count     = base_qs.filter(status="open").count()
    resolved_count = base_qs.filter(status="resolved").count()
    stale_count    = base_qs.filter(status="stale").count()
    all_count      = base_qs.count()
    removed_count  = (
        Finding.objects.filter(client=client, resource_id__in=recently_deleted_ids_lower)
        .count() if recently_deleted_ids_lower else 0
    )

    return render(request, "dashboard/findings.html", {
        "client":               client,
        "clients":              clients,
        "active_page":          "findings",
        "findings":             qs,
        "resource_findings":    resource_findings,
        "total_savings":        (
                                    _capped_total_savings(
                                        qs.filter(status="open").exclude(
                                            finding_type__in=AI_GOVERNANCE_FINDING_TYPES
                                            | {"cost_anomaly"}
                                        ),
                                        _monthly_cost_by_resource(
                                            client,
                                            client.analysis_start or (date.today() - timedelta(days=90)),
                                        ),
                                    )
                                ) if status != "removed" else 0,
        "finding_labels":       FINDING_LABELS,
        "available_types":      list(available_types),
        "filter_status":        status,
        "filter_severity":      severity,
        "filter_type":          ftype,
        "filter_min":           min_savings,
        "open_count":           open_count,
        "resolved_count":       resolved_count,
        "stale_count":          stale_count,
        "all_count":            all_count,
        "removed_count":        removed_count,
        "has_recently_deleted": bool(recently_deleted_ids_lower),
    })


# ── Resolve a finding (AJAX) ─────────────────────────────────────────────────

@login_required
@require_client_access
def resolve_finding(request, slug, finding_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    client = request.client
    finding = get_object_or_404(
        Finding, id=finding_id, client=client, status="open"
    )

    finding.status = "resolved"
    finding.resolved_at = timezone.now()
    finding.save(update_fields=["status", "resolved_at"])

    # Same cache-staleness gap as trigger_live_check (pipeline/views.py) —
    # this changes Finding.status directly in the DB without going through
    # run_pipeline()/run_live_check(), so it needs its own explicit bust.
    _cache_delete(f"summary_{client.id}")
    _cache_delete(f"ai_governance_{client.id}")

    return JsonResponse({
        "success": True,
        "finding_id": finding_id,
    })


# ── Resource deep-dive ────────────────────────────────────────────────────────

@login_required
@require_client_access
def deep_dive(request, slug):
    client  = request.client
    clients = _authorized_clients(request)

    deleted_ids_lower = {
        rid.lower() for rid in
        ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    }

    # All resources across every namespace we collect metrics for —
    # excluding anything the live-check confirmed no longer exists
    resource_rows = (
        DailyMetric.objects
        .filter(client=client)
        .exclude(metric_namespace="")
        .values("resource_name", "resource_id", "metric_namespace")
        .distinct()
        .order_by("metric_namespace", "resource_name")
    )
    resource_rows = [r for r in resource_rows if r["resource_id"].lower() not in deleted_ids_lower]

    # Group by namespace for an optgroup-style dropdown
    grouped_resources = {}
    for r in resource_rows:
        ns = r["metric_namespace"]
        label = NAMESPACE_LABELS.get(ns, ns)
        grouped_resources.setdefault(label, []).append(r)

    selected = request.GET.get("resource", "")
    resource_findings = []
    selected_namespace = ""
    if selected:
        # Show both open and resolved here — on a single resource's page,
        # "this was flagged and you fixed it" is useful context, not noise.
        # Open findings sort first so the actionable ones are seen first.
        resource_findings = Finding.objects.filter(
            client=client, resource_name=selected
        ).order_by("status", "-estimated_monthly_savings_usd")

        match = DailyMetric.objects.filter(
            client=client, resource_name=selected
        ).exclude(metric_namespace="").first()
        if match:
            selected_namespace = match.metric_namespace

    return render(request, "dashboard/deep_dive.html", {
        "client":              client,
        "clients":             clients,
        "active_page":         "deep_dive",
        "grouped_resources":   grouped_resources,
        "selected_resource":   selected,
        "selected_namespace":  selected_namespace,
        "namespace_label":     NAMESPACE_LABELS.get(selected_namespace, selected_namespace),
        "resource_findings":   resource_findings,
        "finding_labels":      FINDING_LABELS,
    })


# ── Deep-dive metrics placeholder (Advisory tier and above) ─────────────────

@login_required
@require_client_access
@require_tier("advisory")
def metrics_deep_dive(request, slug):
    client = get_object_or_404(Client, slug=slug)
    return render(request,
        "dashboard/metrics_placeholder.html",
        {"client": client, "active_page": "metrics"}
    )


# ── All resources (every resource, with or without findings) ────────────────

@login_required
@require_client_access
def all_resources(request, slug):
    client  = request.client
    clients = _authorized_clients(request)

    # Resource IDs confirmed deleted by the live-check — these get
    # excluded from the list entirely. Resources never checked yet
    # (no ResourceStatus row) are assumed to still exist.
    deleted_ids = set(
        ResourceStatus.objects.filter(client=client, exists=False)
        .values_list("resource_id", flat=True)
    )
    deleted_ids_lower = {rid.lower().strip().rstrip("/") for rid in deleted_ids}

    _, monthly_factor, window_label = _window_context(client)

    # Every distinct resource we have metrics for, with its type.
    # .distinct() operates on the raw (un-normalized) resource_id, so
    # historical DailyMetric rows written before collector_metrics.py
    # normalized resource_id at write time can still surface the same
    # physical resource under two different-cased ids here — the loop
    # below guards against that via seen_resource_ids, same pattern the
    # zombie-findings loop already used further down.
    resource_rows = (
        DailyMetric.objects
        .filter(client=client)
        .exclude(metric_namespace="")
        .values("resource_id", "resource_name", "resource_group", "metric_namespace")
        .distinct()
    )

    # Total spend per resource_id, computed once and looked up per row.
    # Azure's Cost Management API and Resource Graph API sometimes return
    # the same resource_id with different casing, so we normalize to
    # lowercase on both sides before building the lookup dict.
    spend_by_resource = {}
    for r in (DailyCost.objects.filter(client=client)
              .exclude(resource_id="")
              .values("resource_id")
              .annotate(total=Sum("billed_cost"))):
        spend_by_resource[r["resource_id"].lower()] = r["total"]

    # Savings opportunity per resource_id, if any OPEN finding exists.
    # Resolved findings shouldn't make a resource look "flagged" anymore.
    savings_by_resource = {}
    for f in (Finding.objects.filter(client=client, status="open")
              .values("resource_id")
              .annotate(total_savings=Sum("estimated_monthly_savings_usd"))):
        savings_by_resource[f["resource_id"].lower()] = f["total_savings"]

    finding_count_by_resource = {}
    for f in (Finding.objects.filter(client=client, status="open")
              .values("resource_id")
              .annotate(n=Count("id"))):
        finding_count_by_resource[f["resource_id"].lower()] = f["n"]

    # Build the combined row list — skip anything confirmed deleted
    rows = []
    seen_resource_ids = set()

    for r in resource_rows:
        rid = r["resource_id"]
        rid_lower = rid.lower().strip().rstrip("/")

        if rid_lower in deleted_ids_lower:
            continue

        if rid_lower in seen_resource_ids:
            # Same physical resource already added under a different
            # casing/trailing-slash spelling of resource_id — cost and
            # finding-count lookups below are already keyed by the same
            # normalized rid_lower regardless of which row got here
            # first, so nothing is lost by skipping this one.
            continue

        seen_resource_ids.add(rid_lower)
        total_cost   = spend_by_resource.get(rid_lower, 0) or 0
        monthly_cost = total_cost * monthly_factor
        savings      = savings_by_resource.get(rid_lower, 0) or 0
        finding_n    = finding_count_by_resource.get(rid_lower, 0)

        rows.append({
            "resource_id":      rid,
            "resource_name":    r["resource_name"],
            "resource_group":   r["resource_group"],
            "namespace":        r["metric_namespace"],
            "namespace_label":  NAMESPACE_LABELS.get(r["metric_namespace"], r["metric_namespace"]),
            "total_cost":       round(total_cost, 2),
            "monthly_cost":     round(monthly_cost, 2),
            "has_findings":     finding_n > 0,
            "finding_count":    finding_n,
            "potential_savings": round(savings, 2),
            "is_zombie":        False,
        })

    # Augment with zombie-type findings that have no DailyMetric rows.
    # Orphaned disks, unattached IPs, empty LBs etc. never get metrics
    # collected so they'd otherwise be invisible in the Resources tab
    # despite having findings. We add them as resource rows here, flagged
    # as zombie resources so the template can style them differently
    # (no spend data, no metrics link — just the finding).
    zombie_findings = (
        Finding.objects
        .filter(client=client, status="open",
                finding_type__in=ZOMBIE_FINDING_TYPES)
        .exclude(resource_id__in=deleted_ids_lower)
        .order_by("-estimated_monthly_savings_usd")
    )
    for f in zombie_findings:
        rid_lower = f.resource_id.lower().strip().rstrip("/")
        if rid_lower in seen_resource_ids:
            continue  # already included via DailyMetric
        seen_resource_ids.add(rid_lower)
        rows.append({
            "resource_id":       f.resource_id,
            "resource_name":     f.resource_name,
            "resource_group":    f.resource_group,
            "namespace":         f.finding_type,
            "namespace_label":   FINDING_LABELS.get(f.finding_type, f.finding_type),
            "total_cost":        spend_by_resource.get(rid_lower, 0) or 0,
            "monthly_cost":      spend_by_resource.get(rid_lower, 0) or 0,
            "has_findings":      True,
            "finding_count":     1,
            "potential_savings": float(f.estimated_monthly_savings_usd or 0),
            "is_zombie":         True,
        })

    # Filters
    namespace_filter = request.GET.get("type", "")
    sort = request.GET.get("sort", "cost_desc")
    show_only = request.GET.get("show", "")  # "flagged" | "clean" | ""

    if namespace_filter:
        rows = [r for r in rows if r["namespace"] == namespace_filter]
    if show_only == "flagged":
        rows = [r for r in rows if r["has_findings"]]
    elif show_only == "clean":
        rows = [r for r in rows if not r["has_findings"]]

    if sort == "cost_desc":
        rows.sort(key=lambda r: r["total_cost"], reverse=True)
    elif sort == "cost_asc":
        rows.sort(key=lambda r: r["total_cost"])
    elif sort == "name":
        rows.sort(key=lambda r: r["resource_name"].lower())
    elif sort == "savings_desc":
        rows.sort(key=lambda r: r["potential_savings"], reverse=True)

    total_spend_all = sum(r["total_cost"] for r in rows)
    flagged_count = sum(1 for r in rows if r["has_findings"])

    available_namespaces = sorted(set(
        r["metric_namespace"] for r in
        DailyMetric.objects.filter(client=client).exclude(metric_namespace="")
        .values("metric_namespace").distinct()
    ))

    return render(request, "dashboard/all_resources.html", {
        "client":               client,
        "clients":              clients,
        "active_page":          "all_resources",
        "rows":                 rows,
        "total_resources":      len(rows),
        "total_spend_all":      round(total_spend_all, 2),
        "window_label":         window_label,
        "flagged_count":        flagged_count,
        "clean_count":          len(rows) - flagged_count,
        "namespace_labels":     NAMESPACE_LABELS,
        "available_namespaces": available_namespaces,
        "filter_namespace":     namespace_filter,
        "filter_sort":          sort,
        "filter_show":          show_only,
    })


# ── Resource history (deleted resources, last 30 days) ──────────────────────

@login_required
@require_client_access
def resource_history(request, slug):
    """
    Resources live_check.py has confirmed deleted from Azure within the
    last 30 days — the counterpart to summary()/findings()/
    ai_governance_page() all hiding deleted resources from their "live"
    views. A resource's history (AIResourcePosture rows, resolved
    Findings) is preserved, not deleted, when it goes away — this page is
    where that history surfaces instead of just disappearing silently.
    """
    client  = request.client
    clients = _authorized_clients(request)

    cutoff = timezone.now() - timedelta(days=30)
    deleted_statuses = (
        ResourceStatus.objects
        .filter(client=client, exists=False, last_checked_at__gte=cutoff)
        .order_by("-last_checked_at")
    )

    rows = []
    for rs in deleted_statuses:
        rid_lower = rs.resource_id.lower().strip().rstrip("/")

        # Best-effort friendly name/group/type — ResourceStatus itself only
        # stores the bare resource_id, so this looks at whichever source
        # last knew about the resource before it was deleted.
        posture = AIResourcePosture.objects.filter(
            client=client, resource_id__iexact=rid_lower
        ).order_by("-scanned_at").first()
        metric = None if posture else DailyMetric.objects.filter(
            client=client, resource_id__iexact=rs.resource_id
        ).first()
        finding = None if (posture or metric) else Finding.objects.filter(
            client=client, resource_id__iexact=rs.resource_id
        ).first()

        if posture:
            resource_name, resource_group, resource_type = (
                posture.resource_name, posture.resource_group, posture.resource_type
            )
        elif metric:
            resource_name, resource_group, resource_type = (
                metric.resource_name, metric.resource_group,
                NAMESPACE_LABELS.get(metric.metric_namespace, metric.metric_namespace),
            )
        elif finding:
            resource_name, resource_group, resource_type = (
                finding.resource_name, finding.resource_group,
                FINDING_LABELS.get(finding.finding_type, finding.finding_type),
            )
        else:
            resource_name, resource_group, resource_type = (
                rs.resource_id.split("/")[-1], "", "",
            )

        savings_resolved = Finding.objects.filter(
            client=client, resource_id__iexact=rs.resource_id, status="resolved",
        ).aggregate(s=Sum("estimated_monthly_savings_usd"))["s"] or 0

        rows.append({
            "resource_id":      rs.resource_id,
            "resource_name":    resource_name,
            "resource_group":   resource_group,
            "resource_type":    resource_type,
            "deleted_at":       rs.last_checked_at,
            "savings_resolved": round(savings_resolved, 2),
        })

    return render(request, "dashboard/resource_history.html", {
        "client":      client,
        "clients":     clients,
        "active_page": "resource_history",
        "rows":        rows,
        "window_days": 30,
    })


# ── Spend overview ────────────────────────────────────────────────────────────

@login_required
@require_client_access
def spend(request, slug):
    client  = request.client
    clients = _authorized_clients(request)

    # Deleted resources' historical billing is excluded from every spend
    # figure on this page (see _live_costs()).
    raw_by_service = (
        _live_costs(client)
        .values("service_name")
        .annotate(total=Sum("billed_cost"))
        .order_by("-total")[:12]
    )

    total_spend = _live_costs(client).aggregate(
        s=Sum("billed_cost"))["s"] or 0

    from core.savings import canonical_monthly_savings
    total_savings = canonical_monthly_savings(client)

    _, monthly_factor, window_label = _window_context(client)

    # Compute monthly average and % of total here — template math
    # (widthratio / divisibleby) is the wrong tool for this and breaks
    # on decimal values.
    by_service = []
    for s in raw_by_service:
        total = s["total"] or 0
        pct = (total / total_spend * 100) if total_spend else 0
        by_service.append({
            "service_name": s["service_name"],
            "total":        round(total, 2),
            "monthly_avg":  round(total * monthly_factor, 2),
            "pct":          round(pct, 1),
        })

    return render(request, "dashboard/spend.html", {
        "client":        client,
        "clients":       clients,
        "active_page":   "spend",
        "by_service":    by_service,
        "total_spend":   round(total_spend, 2),
        "total_cost":    round(total_spend, 2),
        "monthly_avg":   round(total_spend * monthly_factor, 2),
        "window_label":  window_label,
        "total_savings": round(total_savings, 2),
    })


# ── AJAX: spend chart ─────────────────────────────────────────────────────────

@login_required
@require_client_access
def api_spend_chart(request, slug):
    client = request.client
    rows = (
        _live_costs(client)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(total=Sum("billed_cost"))
        .order_by("month")
    )
    return JsonResponse({
        "labels": [r["month"].strftime("%Y-%m") for r in rows],
        "values": [round(r["total"], 2) for r in rows],
    })


# ── AJAX: resource metrics ────────────────────────────────────────────────────

@login_required
@require_client_access
def api_resource_metrics(request, slug, resource_name):
    client = request.client

    def get_series(metric_name):
        qs = (
            DailyMetric.objects
            .filter(client=client, resource_name=resource_name,
                    metric_name=metric_name)
            .order_by("date")
            .values("date", "value")
        )
        return {
            "labels": [str(r["date"]) for r in qs],
            "values": [r["value"] for r in qs],
        }

    # Figure out the namespace so we know which metrics are relevant
    match = DailyMetric.objects.filter(
        client=client, resource_name=resource_name
    ).exclude(metric_namespace="").first()
    namespace = match.metric_namespace if match else ""

    metric_names = NAMESPACE_METRICS.get(namespace, [])
    series = {
        # safe JS-friendly keys, e.g. "Percentage CPU" -> "percentage_cpu"
        m.lower().replace(" ", "_"): get_series(m)
        for m in metric_names
    }
    series["namespace"] = namespace
    return JsonResponse(series)


# ── Client onboarding (staff-only) ───────────────────────────────────────────

GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SLUG_RE = re.compile(r"^[a-z0-9-]+$")

ONBOARD_FORM_FIELDS = [
    "display_name", "slug", "contact_name", "contact_email",
    "report_currency", "tenant_id", "subscription_id",
    "subscription_display_name", "access_group_id",
]


@login_required
@staff_member_required
def onboard_new(request):
    form_data = {f: "" for f in ONBOARD_FORM_FIELDS}
    form_data["report_currency"] = "CAD"
    form_data["subscription_display_name"] = "Production"
    errors = {}

    if request.method == "POST":
        for key in ONBOARD_FORM_FIELDS:
            form_data[key] = request.POST.get(key, "").strip()
        if not form_data["report_currency"]:
            form_data["report_currency"] = "CAD"
        if form_data["report_currency"] not in ("CAD", "USD"):
            errors["report_currency"] = "Choose CAD or USD."
        if not form_data["subscription_display_name"]:
            form_data["subscription_display_name"] = "Production"

        if not form_data["display_name"]:
            errors["display_name"] = "Display name is required."

        slug = form_data["slug"]
        if not slug:
            errors["slug"] = "Slug is required."
        elif not SLUG_RE.match(slug):
            errors["slug"] = "Slug must be lowercase letters, numbers, and hyphens only."
        elif Client.objects.filter(slug=slug).exists():
            errors["slug"] = f"A client with slug '{slug}' already exists."

        if not form_data["contact_name"]:
            errors["contact_name"] = "Contact name is required."

        contact_email = form_data["contact_email"]
        if not contact_email:
            errors["contact_email"] = "Contact email is required."
        else:
            try:
                validate_email(contact_email)
            except DjangoValidationError:
                errors["contact_email"] = "Enter a valid email address."
            else:
                # auth.User.email has no uniqueness constraint, so a
                # contact_email reused across two Client rows would let
                # send_welcome_email's user lookup silently grant one
                # person's login access to both clients' dashboards (see
                # core/email.py::send_welcome_email) — block it here,
                # before a Client row or any secret even exists.
                if Client.objects.filter(
                    contact_email=contact_email, active=True
                ).exclude(slug=slug).exists():
                    errors["contact_email"] = (
                        "This email is already the contact for another "
                        "active client. Each client needs a distinct "
                        "contact email."
                    )

        tenant_id = form_data["tenant_id"]
        if not tenant_id:
            errors["tenant_id"] = "Tenant ID is required."
        elif not GUID_RE.match(tenant_id):
            errors["tenant_id"] = "Tenant ID must be a valid GUID (e.g. 12345678-1234-1234-1234-123456789012)."

        subscription_id = form_data["subscription_id"]
        if not subscription_id:
            errors["subscription_id"] = "Subscription ID is required."
        elif not GUID_RE.match(subscription_id):
            errors["subscription_id"] = "Subscription ID must be a valid GUID."

        access_group_id = form_data["access_group_id"]
        if access_group_id and not GUID_RE.match(access_group_id):
            errors["access_group_id"] = "Access group ID must be a valid GUID."

        if not errors:
            # analysis_start/analysis_end are required (non-null) on Client
            # but aren't part of the onboarding form — default to the same
            # rolling 90-day window core/config.py falls back to when a
            # client's clients.yaml entry omits them.
            client_obj = Client.objects.create(
                slug=slug,
                display_name=form_data["display_name"],
                contact_name=form_data["contact_name"],
                contact_email=contact_email,
                report_currency=form_data["report_currency"],
                onboarding_status="pending_consent",
                active=True,
                analysis_start=date.today() - timedelta(days=90),
                analysis_end=date.today(),
            )

            try:
                from azure.keyvault.secrets import SecretClient
                from azure.identity import DefaultAzureCredential

                kv = SecretClient(
                    vault_url=settings.VAULT_URL,
                    credential=DefaultAzureCredential(),
                )
                kv.set_secret(f"client-{slug}-tenant-id", tenant_id)
                client_obj.tenant_id = tenant_id
                client_obj.save(update_fields=["tenant_id"])
                kv.set_secret(f"client-{slug}-subscription-id", subscription_id)
                if access_group_id:
                    kv.set_secret(f"client-{slug}-access-group-id", access_group_id)

                # The pipeline (core/auth.py) authenticates against every
                # client tenant with the SAME app registration secret — not
                # a per-client one — so it must also land in this client's
                # vault entry as client-{slug}-secret. Sourced from the
                # company vault's ghashem-sso-app-secret ("client_id:
                # client_secret"), never hardcoded (see CLAUDE.md).
                company_kv = SecretClient(
                    vault_url=settings.COMPANY_VAULT_URL,
                    credential=DefaultAzureCredential(),
                )
                sso_secret = company_kv.get_secret("ghashem-sso-app-secret").value
                client_secret = sso_secret.split(":")[1]
                kv.set_secret(f"client-{slug}-secret", client_secret)

                # The pipeline's DB-first config loader (core.config.
                # get_client_config_from_db) requires at least one
                # Subscription row per client — a Client with no
                # Subscription can't run the pipeline at all.
                from core.models import Subscription
                Subscription.objects.create(
                    client=client_obj,
                    subscription_id=subscription_id,
                    display_name=form_data["subscription_display_name"] or "Production"
                )
            except Exception as exc:
                # Never leave a Client record with no secrets behind it —
                # a client with no Key Vault entry is unauthenticatable
                # and would show up in client_list as a broken onboarding.
                client_obj.delete()
                errors["form_error"] = (
                    f"Failed to store secrets in Key Vault: {exc}. "
                    f"Client was not created."
                )
            else:
                messages.success(
                    request,
                    f"Client {form_data['display_name']} created successfully.",
                )
                return redirect("dashboard:onboard_next_steps", slug=slug)

    return render(request, "dashboard/onboard_new.html", {
        "active_page": "client_list",
        "form_data":   form_data,
        "errors":      errors,
    })


@login_required
@staff_member_required
def onboard_next_steps(request, slug):
    client = get_object_or_404(Client, slug=slug)

    # Both instructions on this page (consent + RBAC) are shown together
    # rather than gated one-at-a-time — neither can be verified
    # automatically (they happen in the client's own Azure tenant, done
    # by their admin), so there's no real distinction this app can
    # observe between "waiting for consent" and "waiting for RBAC".
    # Reaching this page is what "ready to work through both steps"
    # means here, so advance straight to pending_pipeline (never
    # regressing an already-active or suspended client).
    if client.onboarding_status in ("pending_consent", "pending_rbac"):
        client.onboarding_status = "pending_pipeline"
        client.save(update_fields=["onboarding_status"])

    from azure.keyvault.secrets import SecretClient
    from azure.identity import DefaultAzureCredential

    kv = SecretClient(vault_url=settings.VAULT_URL, credential=DefaultAzureCredential())
    try:
        tenant_id = kv.get_secret(f"client-{slug}-tenant-id").value
        subscription_id = kv.get_secret(f"client-{slug}-subscription-id").value
    except Exception:
        tenant_id = "[not found in Key Vault]"
        subscription_id = "[not found in Key Vault]"

    consent_url = (
        f"https://login.microsoftonline.com/{tenant_id}"
        f"/adminconsent?client_id={settings.AZURE_APP_CLIENT_ID}"
    )

    # The service principal's object ID is DIFFERENT in each client
    # tenant than in the Ghashem company tenant — settings.GHASHEM_SP_
    # OBJECT_ID is only correct for the company tenant, never for a
    # client's. Resolution order:
    #   1. Live lookup against the client's own tenant (works as soon as
    #      Step 1/admin consent is done — see core.auth.decode_jwt_oid
    #      for why this needs no RBAC role or Graph permission).
    #   2. Fall back to the value stored on Client (populated by
    #      pipeline/runner.py after this client's first successful run).
    #   3. Fall back to None — template shows a warning + placeholder.
    sp_object_id = None
    if tenant_id != "[not found in Key Vault]":
        try:
            from azure.identity import ClientSecretCredential
            from core.auth import decode_jwt_oid

            client_secret = kv.get_secret(f"client-{slug}-secret").value
            client_credential = ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=settings.AZURE_APP_CLIENT_ID,
                client_secret=client_secret,
            )
            token = client_credential.get_token(
                "https://management.azure.com/.default"
            ).token
            sp_object_id = decode_jwt_oid(token)
        except Exception as exc:
            # Most likely cause: admin consent (Step 1) not granted yet,
            # so no service principal exists in the client tenant for
            # this credential to authenticate as.
            log.warning(f"[{slug}] Could not resolve SP object ID live: {exc}")
            sp_object_id = None

    if not sp_object_id:
        sp_object_id = client.client_sp_object_id or None

    script_spid = sp_object_id or "PASTE-SP-OBJECT-ID-HERE"
    rbac_script = f"""$spId = "{script_spid}"
$subId = "{subscription_id}"

foreach ($role in @(
  "Reader",
  "Cost Management Reader",
  "Monitoring Reader",
  "Billing Reader"
)) {{
  az role assignment create `
    --assignee $spId `
    --role $role `
    --scope "/subscriptions/$subId"
  Write-Host "✓ $role"
}}
Write-Host "Done. Notify Ghashem that RBAC is configured."
"""

    return render(request, "dashboard/onboard_next_steps.html", {
        "active_page": "client_list",
        "client":      client,
        "consent_url": consent_url,
        "sp_object_id": sp_object_id,
        "rbac_script": rbac_script,
    })


# ── Client management list (staff-only) ──────────────────────────────────────

@login_required
@staff_member_required
def client_list(request):
    from core.billing import calculate_monthly_fee

    clients_data = []
    for client in Client.objects.all().order_by("display_name"):
        last_run = PipelineRun.objects.filter(
            client=client
        ).order_by("-started_at").first()

        open_findings = Finding.objects.filter(
            client=client, status="open"
        ).count()

        from core.savings import canonical_monthly_savings
        monthly_savings = canonical_monthly_savings(client)

        fee = calculate_monthly_fee(client)

        clients_data.append({
            "client":          client,
            "last_run":        last_run,
            "open_findings":   open_findings,
            "monthly_savings": monthly_savings,
            "monthly_fee":     fee,
        })

    return render(request, "dashboard/client_list.html", {
        "active_page":  "client_list",
        "clients_data": clients_data,
        "tier_choices": Client.TIER_CHOICES,
    })


@login_required
@staff_member_required
def set_tier(request, slug):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    client = get_object_or_404(Client, slug=slug)
    new_tier = request.POST.get("tier", "").strip()
    override = request.POST.get(
        "tier_override_monthly", ""
    ).strip()

    valid_tiers = [t[0] for t in Client.TIER_CHOICES]
    if new_tier and new_tier in valid_tiers:
        client.tier = new_tier

    if override:
        try:
            from decimal import Decimal, InvalidOperation
            client.tier_override_monthly = Decimal(override)
        except InvalidOperation:
            pass  # ignore invalid decimal input
    elif "tier_override_monthly" in request.POST \
         and not override:
        # Empty string submitted = clear the override
        client.tier_override_monthly = None

    client.save(update_fields=["tier",
                                "tier_override_monthly"])

    messages.success(
        request,
        f"{client.display_name} updated to "
        f"{client.get_tier_display()} tier."
    )
    return redirect("dashboard:client_list")


@login_required
@staff_member_required
def delete_client(request, slug):
    """
    Full cleanup of one client: DB rows (Client + everything that
    cascades via on_delete=CASCADE — Subscription, Finding, DailyCost,
    DailyMetric, PipelineRun, ResourceStatus, UserClientAccess), its
    four Key Vault secrets, and its clients.yaml entry if present.

    AJAX-only (called from client_list.html's confirmation modal) —
    returns JSON, not a redirect, so the row can be removed from the
    table without a full page reload.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        # STEP 1 — AccessLog.client_slug is a plain CharField, not a
        # Client FK (see core/models.py), so it does NOT cascade with
        # the Client delete in STEP 2 and must be cleaned up explicitly.
        AccessLog.objects.filter(client_slug=slug).delete()

        # STEP 2 — Cascades to every other Client-FK'd model.
        client = get_object_or_404(Client, slug=slug)
        display_name = client.display_name
        client.delete()

        # STEP 3 — Key Vault cleanup. Wrapped so a Key Vault failure
        # can never leave the DB half-deleted — the DB rows are already
        # gone by this point regardless of what happens here.
        try:
            from azure.keyvault.secrets import SecretClient
            from azure.identity import DefaultAzureCredential
            from azure.core.exceptions import ResourceNotFoundError

            kv = SecretClient(
                vault_url=settings.VAULT_URL,
                credential=DefaultAzureCredential(),
            )
            secret_names = [
                f"client-{slug}-tenant-id",
                f"client-{slug}-subscription-id",
                f"client-{slug}-secret",
                f"client-{slug}-access-group-id",
            ]
            for secret_name in secret_names:
                try:
                    kv.begin_delete_secret(secret_name).wait()
                except ResourceNotFoundError:
                    # Never existed (e.g. access-group-id is optional) —
                    # nothing to clean up, not an error.
                    continue
                except Exception as exc:
                    log.warning(
                        f"[{slug}] Failed to delete secret "
                        f"{secret_name}: {exc}"
                    )
                    continue

                try:
                    kv.purge_deleted_secret(secret_name)
                except Exception as exc:
                    # Soft-deleted but not purged — it'll auto-purge
                    # after the vault's retention period regardless.
                    log.warning(
                        f"[{slug}] Deleted but could not purge secret "
                        f"{secret_name}: {exc}"
                    )
        except Exception as exc:
            log.warning(f"[{slug}] Key Vault cleanup failed: {exc}")

        # STEP 4 — Remove from clients.yaml, if present. Non-fatal —
        # onboarding wrote a Client row with no matching YAML entry for
        # any client onboarded through the DB-first path (see
        # core/config.py::get_client_config_from_db), so a missing
        # entry here is the normal case, not an error.
        try:
            import yaml
            yaml_path = settings.CONFIG_DIR / "clients.yaml"
            with open(yaml_path, "r", encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}

            clients_raw = data.get("clients", []) or []
            updated = [c for c in clients_raw if c.get("slug") != slug]
            data["clients"] = updated

            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        except Exception as exc:
            log.warning(f"[{slug}] Could not remove from clients.yaml: {exc}")

        return JsonResponse({
            "success": True,
            "message": f"{display_name} deleted successfully.",
            "slug": slug,
        })

    except Exception as e:
        log.error(f"[{slug}] delete_client failed: {e}")
        return JsonResponse({"success": False, "message": str(e)}, status=500)


# ── Monthly executive brief (on-demand download) ─────────────────────────────

@login_required
@require_client_access
def download_brief(request, slug):
    client = request.client
    from core.brief import generate_monthly_brief

    run_obj = PipelineRun.objects.filter(client=client).order_by("-started_at").first()
    pdf_bytes = generate_monthly_brief(client, run_obj)

    filename = f"Ghashem_{client.slug}_{timezone.localdate().strftime('%Y_%m')}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
