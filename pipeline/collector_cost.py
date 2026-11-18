"""
Cost collector — calls the live Cost Management Query API.

Includes retry-with-exponential-backoff for 429 (throttling) responses,
since Cost Management enforces rate limits that are easy to hit during
active development (repeated manual pipeline runs) even though they're
rarely an issue in normal scheduled production use.
"""

import time
import logging
from datetime import date, datetime
from azure.core.exceptions import HttpResponseError
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryDefinition, QueryTimePeriod, QueryDataset,
    QueryAggregation, QueryGrouping, TimeframeType, GranularityType,
)
from core.models import DailyCost, Client

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 10  # doubles each retry: 10, 20, 40, 80, 160


def _query_with_retry(cost_client: CostManagementClient, scope: str, query: QueryDefinition):
    """
    Calls the Cost Management query API with exponential backoff on 429.
    Respects the Retry-After header if Azure provides one, otherwise
    falls back to doubling delay.
    """
    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return cost_client.query.usage(scope=scope, parameters=query)

        except HttpResponseError as exc:
            last_exception = exc

            if exc.status_code != 429:
                # Not a throttling error — don't retry, raise immediately
                raise

            # Try to honor Azure's suggested wait time if present
            retry_after = None
            if exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")

            if retry_after:
                delay = int(retry_after)
            else:
                delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))

            log.warning(
                f"  Cost Management API throttled (429). "
                f"Attempt {attempt}/{MAX_RETRIES}. Waiting {delay}s before retry..."
            )
            time.sleep(delay)

    # All retries exhausted
    log.error(f"  Cost Management API still throttled after {MAX_RETRIES} attempts. Giving up.")
    raise last_exception


def _build_query(start_dt: datetime, end_dt: datetime, focus: bool) -> QueryDefinition:
    """
    focus=True adds the FOCUS-aligned grouping dimensions (ServiceCategory,
    ChargeCategory, ProviderName, PricingCategory) on top of the existing
    ones. Not every subscription/API version accepts these — see the
    400-fallback in collect_costs().
    """
    grouping = [
        QueryGrouping(type="Dimension", name="ServiceName"),
        QueryGrouping(type="Dimension", name="ResourceGroupName"),
        QueryGrouping(type="Dimension", name="ResourceId"),
    ]
    if focus:
        grouping += [
            QueryGrouping(type="Dimension", name="ServiceCategory"),
            QueryGrouping(type="Dimension", name="ChargeCategory"),
            QueryGrouping(type="Dimension", name="ProviderName"),
            QueryGrouping(type="Dimension", name="PricingCategory"),
        ]

    return QueryDefinition(
        type="ActualCost",
        timeframe=TimeframeType.CUSTOM,
        time_period=QueryTimePeriod(from_property=start_dt, to=end_dt),
        dataset=QueryDataset(
            granularity=GranularityType.DAILY,
            aggregation={
                "totalCost": QueryAggregation(name="Cost", function="Sum"),
            },
            grouping=grouping,
        ),
    )


def collect_costs(client_obj: Client, ctx, subscription_id: str,
                  start_date: date, end_date: date) -> int:
    """
    Pulls daily cost data for one subscription and saves to DailyCost.
    Returns the number of rows saved.
    """
    cost_client = CostManagementClient(
        credential=ctx.credential,
        subscription_id=subscription_id,
    )
    scope = f"/subscriptions/{subscription_id}"

    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    end_dt   = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)

    try:
        result = _query_with_retry(cost_client, scope, _build_query(start_dt, end_dt, focus=True))
    except HttpResponseError as exc:
        if exc.status_code == 400:
            log.warning(f"[{client_obj.slug}] FOCUS dimensions not supported "
                        f"— falling back to legacy Cost Management query")
            result = _query_with_retry(cost_client, scope, _build_query(start_dt, end_dt, focus=False))
        else:
            raise

    columns = [col.name for col in result.columns]
    # Only trust/apply the ChargeCategory-based Tax/Credit/Adjustment filter
    # when the FOCUS query actually succeeded and returned that column —
    # after a fallback to the legacy query, there's no ChargeCategory data
    # to filter on at all.
    has_charge_category = "ChargeCategory" in columns
    saved = 0

    for row in result.rows:
        data = dict(zip(columns, row))
        raw_date = str(data.get("UsageDate", ""))
        if len(raw_date) == 8:
            row_date = datetime.strptime(raw_date, "%Y%m%d").date()
        else:
            continue

        resource_id = str(data.get("ResourceId", "")).lower()
        if "providers" not in resource_id or not resource_id.startswith("/subscriptions/"):
            continue

        if has_charge_category:
            charge = data.get("ChargeCategory", "")
            if charge and charge in ("Tax", "Credit", "Adjustment"):
                continue

        DailyCost.objects.update_or_create(
            client=client_obj,
            date=row_date,
            subscription_id=subscription_id,
            service_name=data.get("ServiceName", "")[:300],
            resource_group=str(data.get("ResourceGroupName", ""))[:300],
            resource_id=resource_id,
            defaults={
                "billed_cost": float(data.get("Cost", 0) or 0),
                "billing_currency": str(data.get("Currency", client_obj.report_currency)),
                "effective_cost": float(data.get("EffectiveCost", 0) or 0),
                "service_category": str(data.get("ServiceCategory", "")),
                "charge_type": str(data.get("ChargeCategory", "")),
                "provider": str(data.get("ProviderName", "Microsoft")),
                "pricing_category": str(data.get("PricingCategory", "")),
            },
        )
        saved += 1

    log.info(f"[{client_obj.slug}] Cost rows saved: {saved}")
    return saved
