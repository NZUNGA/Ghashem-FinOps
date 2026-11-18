"""
core/sso_pipeline.py

Custom python-social-auth pipeline step — grants UserClientAccess
(viewer role) only if the authenticated user is a member of the
Azure AD security group designated for this client.
"""

import base64
import json
import logging
import requests
from core.models import UserClientAccess, Client

log = logging.getLogger(__name__)


def _extract_tid_from_jwt(token: str) -> str:
    """
    Extracts the tenant ID (tid) from a Microsoft JWT access token.
    Microsoft's Graph /me endpoint doesn't include tid in the profile
    response — it's embedded in the JWT claims payload instead.
    JWT format: header.payload.signature (base64url encoded, no padding)
    """
    if not token:
        return ""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        # Add padding so base64 decodes cleanly
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        tid = claims.get("tid", "")
        log.info(f"SSO pipeline: decoded JWT claims keys = {list(claims.keys())}")
        return tid
    except Exception as exc:
        log.warning(f"SSO pipeline: JWT decode failed: {exc}")
        return ""


def auto_grant_client_access(backend, user, response, *args, **kwargs):
    """
    Called by social-auth after the Django user is created/retrieved.

    Wrapped end-to-end: this runs as a pipeline step in the middle of the
    login flow, so an unhandled exception here (Key Vault hiccup, Graph
    API error, etc.) would otherwise turn an already-successful
    authentication into a 500 for the user. Worst case on failure: the
    user logs in but gets no client access yet — an admin can grant it
    manually — which is far better than blocking login entirely.
    """
    try:
        log.info(f"SSO pipeline: running for user={user.username} email={user.email}")
        log.info(f"SSO pipeline: response keys = {list(response.keys())}")

        # Extract access token first — needed both for tenant ID and group check
        access_token = (
            kwargs.get("access_token")
            or response.get("access_token")
            or _get_token_from_backend(backend)
        )
        log.info(f"SSO pipeline: access_token present = {bool(access_token)}")

        # tid is in the JWT claims, not in the Graph /me profile response
        tenant_id = response.get("tid") or _extract_tid_from_jwt(access_token)
        log.info(f"SSO pipeline: tenant_id = {tenant_id}")

        if not tenant_id:
            log.warning("SSO pipeline: could not determine tenant_id — skipping")
            return

        # Find the matching client and group
        client_obj, group_id = _find_client_and_group(tenant_id)
        log.info(f"SSO pipeline: client_obj = {client_obj}, group_id = {group_id!r}")

        if client_obj is None:
            log.warning(f"SSO pipeline: tenant {tenant_id} not matched to any client")
            return

        if not group_id:
            log.warning(
                f"SSO pipeline: no group_id for {client_obj.slug} — "
                f"granting access by tenant match only. "
                f"Store client-{client_obj.slug}-access-group-id in Key Vault to enable group check."
            )
            _do_grant(user, client_obj, "sso-tenant-only")
            return

        # Check group membership
        if not access_token:
            log.warning("SSO pipeline: no access_token for group check — granting by tenant only")
            _do_grant(user, client_obj, "sso-tenant-only")
            return

        is_member = _is_group_member(access_token, group_id)
        log.info(f"SSO pipeline: is_member of {group_id} = {is_member}")

        if not is_member:
            log.warning(f"SSO pipeline: {user.email} is NOT in group {group_id} — access denied")
            return

        _do_grant(user, client_obj, "sso")
    except Exception as e:
        log.error(
            f"SSO pipeline: auto_grant_client_access failed for user {user}: {e}",
            exc_info=True,
        )
        # Never let pipeline errors block login — user logs in but may
        # have no client access yet; an admin can grant it manually.


def _do_grant(user, client_obj, granted_by: str):
    _, created = UserClientAccess.objects.get_or_create(
        user=user,
        client=client_obj,
        defaults={
            "role": "viewer",
            "granted_by": None,  # SSO grants have no human granter — null is correct
        },
    )
    if created:
        log.info(f"SSO pipeline: GRANTED viewer to {user.email} for {client_obj.slug} ({granted_by})")
    else:
        log.info(f"SSO pipeline: {user.email} already has access to {client_obj.slug}")


def _get_token_from_backend(backend):
    try:
        return backend.get_session_value("access_token")
    except Exception:
        pass
    try:
        return backend.strategy.session_get("access_token")
    except Exception:
        pass
    return None


def _find_client_and_group(tenant_id: str):
    # YAML path — kept for backwards compat with any client still driven by
    # clients.yaml. In practice this is empty today (every client is
    # DB-onboarded via /onboard/new/), so this loop costs one Key Vault call
    # per YAML-listed client and then falls through to the DB path below.
    try:
        from core.config import load_config
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential

        config = load_config()
        log.info(f"SSO pipeline: vault_url = {config.vault_url}")
        log.info(f"SSO pipeline: active clients = {[c.slug for c in config.active_clients()]}")

        kv = SecretClient(vault_url=config.vault_url, credential=DefaultAzureCredential())

        for client_cfg in config.active_clients():
            try:
                stored_tid = kv.get_secret(f"client-{client_cfg.slug}-tenant-id").value
                log.info(f"SSO pipeline: {client_cfg.slug} stored tid = {stored_tid}")

                if stored_tid and stored_tid.lower() == tenant_id.lower():
                    log.info(f"SSO pipeline: matched client {client_cfg.slug}")

                    group_id = ""
                    try:
                        group_id = kv.get_secret(
                            f"client-{client_cfg.slug}-access-group-id"
                        ).value or ""
                        log.info(f"SSO pipeline: group_id = {group_id!r}")
                    except Exception as e:
                        log.warning(f"SSO pipeline: no access-group-id for {client_cfg.slug}: {e}")

                    try:
                        client_obj = Client.objects.get(slug=client_cfg.slug)
                        return client_obj, group_id
                    except Client.DoesNotExist:
                        log.warning(f"SSO pipeline: Client row not found for {client_cfg.slug} — run the pipeline first")
                        return None, None

            except Exception as e:
                log.warning(f"SSO pipeline: error reading tenant-id for {client_cfg.slug}: {e}")
                continue

    except Exception as exc:
        log.warning(f"SSO pipeline: YAML tenant match failed: {exc}")

    # DB fallback — covers every client onboarded via /onboard/new/, which
    # writes Client.tenant_id directly (see dashboard/views.py::onboard_new).
    # A single indexed lookup, not a per-client Key Vault round trip, so this
    # stays cheap on every login regardless of how many clients exist.
    if tenant_id:
        try:
            db_client = Client.objects.filter(
                tenant_id__iexact=tenant_id, active=True
            ).first()
        except Exception as exc:
            log.warning(f"SSO pipeline: DB tenant match failed: {exc}")
            return None, None

        if db_client is not None:
            log.info(f"SSO pipeline: matched client {db_client.slug} via DB tenant_id")

            group_id = ""
            try:
                from azure.keyvault.secrets import SecretClient
                from azure.identity import DefaultAzureCredential
                from django.conf import settings

                kv = SecretClient(
                    vault_url=settings.VAULT_URL,
                    credential=DefaultAzureCredential(),
                )
                group_id = kv.get_secret(
                    f"client-{db_client.slug}-access-group-id"
                ).value or ""
                log.info(f"SSO pipeline: group_id = {group_id!r}")
            except Exception as e:
                log.warning(f"SSO pipeline: no access-group-id for {db_client.slug}: {e}")

            return db_client, group_id

    return None, None


def _is_group_member(access_token: str, group_id: str) -> bool:
    try:
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/checkMemberObjects",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"ids": [group_id]},
            timeout=5,
        )
        log.info(f"SSO pipeline: Graph checkMemberObjects status = {resp.status_code}")

        if resp.status_code == 403:
            log.warning("SSO pipeline: Graph 403 — missing GroupMember.Read.All permission")
            return False

        resp.raise_for_status()
        result = resp.json().get("value", [])
        log.info(f"SSO pipeline: checkMemberObjects result = {result}")
        return group_id in result

    except requests.Timeout:
        log.warning("SSO pipeline: group check timed out")
        return False
    except Exception as exc:
        log.warning(f"SSO pipeline: group check failed: {exc}")
        return False
