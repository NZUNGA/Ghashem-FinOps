"""
Reserved Instance / commitment-discount finder.

Surfaces Azure Advisor's own Cost-category recommendations rather than
computing reservation math independently — Advisor already has access to
Azure's internal historical-usage-vs-reservation-pricing models that
aren't otherwise exposed via Cost Management or Resource Graph. This
analyzer's only job is to translate Advisor's reservation-shaped
recommendations into Finding rows using the same
(client, run_obj, resource_id, ...) convention as every other analyzer,
via the shared pipeline.finding_utils.upsert_finding() helper.

Unlike the other analyzers, this one makes a live Azure call per
subscription (Advisor has no local cache in this app) rather than only
reading from the DB, so credentials are loaded here the same way
pipeline/runner.py loads them for the collectors — client_slug, a
per-client Key Vault lookup via core.auth.load_client_context, and the
single global app registration's client ID / vault URL from settings.
Every subscription's block is wrapped in its own try/except: an Advisor
outage, missing RBAC role, or SDK error must never fail the pipeline
run that calls this analyzer.
"""

import logging
from django.conf import settings
from azure.mgmt.advisor import AdvisorManagementClient
from core.auth import load_client_context
from core.models import Client, PipelineRun
from pipeline.finding_utils import upsert_finding

log = logging.getLogger(__name__)


def _short_description_text(rec) -> str:
    """
    ResourceRecommendationBase.short_description is a ShortDescription
    object with separate .problem/.solution strings, not a single
    string — combine both so a 'reservation' match in either half is
    caught, and guard against a missing short_description entirely.
    """
    sd = getattr(rec, "short_description", None)
    if sd is None:
        return ""
    problem  = getattr(sd, "problem", "") or ""
    solution = getattr(sd, "solution", "") or ""
    return f"{problem} {solution}"


def run(client: Client, run_obj: PipelineRun) -> int:
    app_client_id = getattr(settings, "AZURE_APP_CLIENT_ID", "")
    vault_url = getattr(settings, "VAULT_URL", "")

    created = 0

    for sub in client.subscriptions.all():
        try:
            ctx = load_client_context(
                client_slug=client.slug,
                app_client_id=app_client_id,
                vault_url=vault_url,
                subscription_id=sub.subscription_id,
            )
            advisor_client = AdvisorManagementClient(
                credential=ctx.credential,
                subscription_id=sub.subscription_id,
            )

            for rec in advisor_client.recommendations.list():
                if getattr(rec, "category", "") != "Cost":
                    continue

                if "reservation" not in _short_description_text(rec).lower():
                    continue

                resource_metadata = getattr(rec, "resource_metadata", None)
                resource_id = (getattr(resource_metadata, "resource_id", "") or "").strip()
                if not resource_id:
                    continue

                extended_properties = getattr(rec, "extended_properties", None) or {}
                try:
                    annual_saving = float(extended_properties.get("annualSavingsAmount", 0) or 0)
                except (TypeError, ValueError):
                    annual_saving = 0.0

                if annual_saving <= 0:
                    continue

                resource_name = resource_id.rstrip("/").split("/")[-1]
                monthly_saving = round(annual_saving / 12, 2)

                upsert_finding(
                    client=client, run_obj=run_obj,
                    resource_id=resource_id.lower(),
                    resource_name=resource_name,
                    resource_group="",
                    finding_type="reservation_opportunity",
                    analyzer="reservations",
                    severity="medium",
                    monthly_savings=monthly_saving,
                    evidence=(
                        f"Azure Advisor recommends a Reserved Instance "
                        f"for '{resource_name}'. Estimated saving: "
                        f"${monthly_saving:.2f}/month "
                        f"(${annual_saving:.0f}/year) vs on-demand pricing."
                    ),
                    action=(
                        "Purchase a 1-year Reserved Instance for this "
                        "resource. Reservations save 33-72% vs on-demand "
                        "for continuously-running workloads."
                    ),
                    extra_data={
                        "annual_saving_usd": round(annual_saving, 2),
                    },
                )
                created += 1

        except Exception as exc:
            log.warning(
                f"[{client.slug}] Reservation finder failed for subscription "
                f"{sub.subscription_id}: {exc}"
            )
            continue

    return created
