import base64
import json
from dataclasses import dataclass
from azure.identity import DefaultAzureCredential, CertificateCredential
from azure.keyvault.secrets import SecretClient


def decode_jwt_oid(access_token: str) -> str | None:
    """
    Extracts the 'oid' claim (object ID of the calling service principal,
    in the tenant that issued the token) from an Entra ID access token,
    without verifying the signature — safe here because we're reading a
    token issued directly to us moments ago over TLS, not validating a
    token presented by an untrusted caller.

    Used to resolve the Ghashem app's service principal object ID in a
    given client tenant — that object ID differs per tenant (a new SP is
    created in each tenant on admin consent) and Entra ID has no simpler
    lookup for it that doesn't require a Microsoft Graph API permission
    this app has never needed and isn't known to have configured. Token
    issuance itself only requires the SP to exist (i.e. admin consent
    done) and a valid credential — no RBAC role assignment or Graph
    permission needed — so this works as soon as Step 1 (consent) is
    complete, before Step 2 (RBAC) even runs.
    """
    try:
        payload_b64 = access_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return payload.get("oid")
    except Exception:
        return None


@dataclass
class ClientContext:
    client_slug: str
    tenant_id: str
    subscription_id: str
    credential: object


def load_client_context(client_slug: str, app_client_id: str,
                        vault_url: str, subscription_id: str) -> ClientContext:
    """
    Retrieves credentials from Key Vault and returns a ClientContext
    ready for Azure SDK calls.
    """
    local_cred = DefaultAzureCredential()
    secrets = SecretClient(vault_url=vault_url, credential=local_cred)

    # tenant_id MUST be looked up per-client from kv-ghashem-dev, never
    # hardcoded — per CLAUDE.md's two-tenant model, one multitenant app
    # registration (in the Ghashem company tenant) serves many client
    # tenants, each with its own service principal. A literal tenant_id
    # here would silently authenticate every client against whichever
    # tenant that literal belongs to instead of their own.
    tenant_id = secrets.get_secret(f"client-{client_slug}-tenant-id").value

    # Try certificate first, fall back to client secret
    try:
        cert_bytes = secrets.get_secret(f"client-{client_slug}-cert").value.encode("utf-8")
        credential = CertificateCredential(
            tenant_id=tenant_id,
            client_id=app_client_id,
            certificate_data=cert_bytes,
        )
    except Exception:
        from azure.identity import ClientSecretCredential
        secret_val = secrets.get_secret(f"client-{client_slug}-secret").value
        credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=app_client_id,
            client_secret=secret_val,
        )

    return ClientContext(
        client_slug=client_slug,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        credential=credential,
    )
