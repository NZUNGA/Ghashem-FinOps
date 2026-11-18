"""
Tenant isolation enforcement.

Every view that takes a client slug from the URL MUST use
@require_client_access. This is the single chokepoint that prevents
one client's data from being viewable by a user who isn't explicitly
granted access to it â the textbook IDOR (Insecure Direct Object
Reference) vulnerability a pentest will specifically probe for.

Usage:
    @login_required
    @require_client_access
    def summary(request, slug):
        # `request.client` is now available, already validated
        ...

Design choices, explained:
- Denies by default. No row in UserClientAccess = no access, full stop.
  Superusers are the only exception (for your own admin convenience),
  and that exception is itself logged.
- Logs every attempt, allowed or denied, to AccessLog. A pentest report
  will check whether you can answer "who tried to access what" â this
  is how you answer it.
- Returns 404, not 403, for denied access to a client slug that exists.
  This avoids confirming to an attacker that a given client slug is
  valid at all (an information-disclosure best practice).
"""

import functools
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404
from core.models import Client, UserClientAccess, AccessLog


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR", "")
    # Strip port if present (e.g. Azure sends "1.2.3.4:56789")
    if ip and ":" in ip and not ip.startswith("["):
        ip = ip.split(":")[0]
    return ip or None


def _log_access(request, client_slug: str, allowed: bool):
    AccessLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        client_slug=client_slug,
        path=request.path,
        ip_address=_client_ip(request),
        allowed=allowed,
    )


def require_client_access(view_func):
    """
    Validates that request.user has a UserClientAccess grant for the
    `slug` URL kwarg before calling the view. Attaches the resolved
    Client object as request.client so views don't re-query it.
    """
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        client = get_object_or_404(Client, slug=slug)

        has_access = (
            request.user.is_superuser
            or UserClientAccess.objects.filter(
                user=request.user, client=client
            ).exists()
        )

        _log_access(request, slug, allowed=has_access)

        if not has_access:
            # 404, not 403 â don't confirm the slug exists to a user
            # who isn't authorized to know that.
            raise Http404()

        request.client = client
        return view_func(request, slug, *args, **kwargs)

    return wrapper


def require_client_admin(view_func):
    """
    Stricter variant for state-changing actions (triggering pipeline
    runs, modifying client config). Requires role='admin', not just
    any grant.
    """
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        client = get_object_or_404(Client, slug=slug)

        is_admin = (
            request.user.is_superuser
            or UserClientAccess.objects.filter(
                user=request.user, client=client, role="admin"
            ).exists()
        )

        _log_access(request, slug, allowed=is_admin)

        if not is_admin:
            raise Http404()

        request.client = client
        return view_func(request, slug, *args, **kwargs)

    return wrapper


def staff_member_required(view_func):
    """
    Gates onboarding/admin views (client onboarding, the client
    management list) to staff users only.

    Deliberately NOT django.contrib.admin.views.decorators.staff_member_
    required — that one redirects non-staff users to the admin login
    page (302), which silently lets an authenticated non-staff user
    "succeed" their way to a login prompt instead of being told no.
    These are internal Ghashem-staff-only pages, not client-facing, so a
    non-staff request here should be denied outright (403), matching
    every other access-control decorator in this module. Always stack
    under @login_required so request.user is already authenticated by
    the time this runs.
    """
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied()
        return view_func(request, *args, **kwargs)

    return wrapper


TIER_RANK = {"visibility": 1, "advisory": 2, "managed": 3}


def require_tier(minimum_tier):
    """
    Decorator that enforces a minimum service tier.
    Returns upgrade_required.html with 403 status if
    client tier is below the required minimum.
    Superusers bypass the check entirely.
    """
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, slug, *args, **kwargs):
            from django.conf import settings
            from django.shortcuts import get_object_or_404, render
            from core.models import Client
            client = get_object_or_404(Client, slug=slug)
            client_rank = TIER_RANK.get(client.tier, 0)
            required_rank = TIER_RANK.get(minimum_tier, 99)
            if client_rank < required_rank and \
               not request.user.is_superuser:
                return render(
                    request,
                    "dashboard/upgrade_required.html",
                    {
                        "client": client,
                        "required_tier": minimum_tier,
                        "current_tier": client.tier,
                        "tier_display": dict(
                            Client.TIER_CHOICES
                        ),
                        "contact_email": getattr(
                            settings,
                            "GHASHEM_CONTACT_EMAIL",
                            "support@ghashem.ca"
                        ),
                    },
                    status=403
                )
            return view_func(request, slug, *args, **kwargs)
        return wrapper
    return decorator

