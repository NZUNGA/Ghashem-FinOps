from dataclasses import dataclass, field
from pathlib import Path
from datetime import date
import yaml
from django.conf import settings


@dataclass
class SubscriptionConfig:
    id: str
    display_name: str


@dataclass
class CostExportConfig:
    storage_account:  str
    container:        str
    directory_prefix: str


@dataclass
class ClientConfig:
    slug:            str
    display_name:    str
    active:          bool
    analysis_start:  date
    analysis_end:    date

    @classmethod
    def rolling_window(cls, days: int = 90) -> tuple:
        """Return (start, end) as a rolling window ending today."""
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=days)
        return start, end
    subscriptions:   list
    focus_areas:     list
    report_currency: str
    cost_export:     CostExportConfig = None
    # Azure AD security group Object ID whose members get SSO viewer access.
    # Set in clients.yaml as access_group_id, stored in Key Vault as
    # client-{slug}-access-group-id. If empty, SSO login is denied for
    # this client (fail closed).
    access_group_id: str = ""


@dataclass
class GhashemConfig:
    app_client_id: str
    vault_url: str
    clients: list

    def active_clients(self):
        return [c for c in self.clients if c.active]

    def get_client(self, slug: str):
        matches = [c for c in self.clients if c.slug == slug]
        if not matches:
            raise ValueError(f"No client with slug '{slug}'")
        return matches[0]


def load_config(path: Path = None) -> GhashemConfig:
    if path is None:
        path = settings.CONFIG_DIR / "clients.yaml"
    with open(path, encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)
    clients = []
    for c in raw["clients"]:
        subs = [SubscriptionConfig(id=s["id"], display_name=s["display_name"])
                for s in c["subscriptions"]]

        cost_export = None
        if "cost_export" in c:
            ce = c["cost_export"]
            cost_export = CostExportConfig(
                storage_account=ce["storage_account"],
                container=ce["container"],
                directory_prefix=ce["directory_prefix"],
            )

        clients.append(ClientConfig(
            slug=c["slug"],
            display_name=c["display_name"],
            active=c["active"],
            analysis_start=date.fromisoformat(c["analysis_start"]) if c.get("analysis_start") else (date.today() - __import__('datetime').timedelta(days=90)),
            analysis_end=date.fromisoformat(c["analysis_end"]) if c.get("analysis_end") else date.today(),
            subscriptions=subs,
            focus_areas=c["focus_areas"],
            report_currency=c["report_currency"],
            cost_export=cost_export,
            access_group_id=c.get("access_group_id", ""),
        ))
    return GhashemConfig(
        app_client_id=raw["app_client_id"],
        vault_url=raw["vault_url"],
        clients=clients,
    )


def get_client_config_from_db(slug: str) -> ClientConfig:
    """
    Builds a ClientConfig from the database instead of clients.yaml.

    Returns exactly the same ClientConfig dataclass load_config().
    get_client(slug) returns for the YAML path — NOT a GhashemConfig.
    ClientConfig has no app_client_id/vault_url fields (those live on
    the separate GhashemConfig wrapper, sourced from raw["app_client_id"]/
    raw["vault_url"] in the YAML path); callers using this DB path must
    source app_client_id/vault_url from settings.AZURE_APP_CLIENT_ID/
    settings.VAULT_URL directly, the same values load_client_context()
    already needs regardless of which config path is used.

    Raises ValueError if the slug isn't in the DB, or if the client has
    no Subscription rows — same failure mode/exception type as the YAML
    path's GhashemConfig.get_client(), so callers can catch ValueError
    once and fall back to YAML without caring which case triggered it.
    """
    from datetime import timedelta
    from core.models import Client, Subscription

    try:
        client = Client.objects.get(slug=slug)
    except Client.DoesNotExist:
        raise ValueError(f"No client with slug '{slug}'")

    # Rolling 90-day window if dates not set (analysis_start/analysis_end
    # are nullable — see migration 0009).
    today = date.today()
    analysis_start = client.analysis_start or (today - timedelta(days=90))
    analysis_end = client.analysis_end or today

    subs = list(
        Subscription.objects.filter(client=client)
        .values("subscription_id", "display_name")
    )
    if not subs:
        raise ValueError(
            f"Client '{slug}' has no subscription records. "
            f"Re-register through /onboard/new/ or add a "
            f"Subscription row manually."
        )

    return ClientConfig(
        slug=client.slug,
        display_name=client.display_name,
        active=client.active,
        analysis_start=analysis_start,
        analysis_end=analysis_end,
        subscriptions=[
            SubscriptionConfig(id=s["subscription_id"], display_name=s["display_name"])
            for s in subs
        ],
        focus_areas=[],
        report_currency=client.report_currency,
    )

