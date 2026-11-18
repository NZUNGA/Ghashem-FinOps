from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.access import require_client_access
from core.models import Client
from .models import Contract, ContractRenewal

CONTRACT_FORM_FIELDS = [
    "vendor_name", "product_name", "annual_cost", "currency",
    "renewal_date", "owner_name", "owner_email", "payment_method", "notes",
]


@login_required
@require_client_access
def contract_list(request, slug):
    client = get_object_or_404(Client, slug=slug)
    contracts = Contract.objects.filter(client=client, status="active")
    today = date.today()

    expiring_soon = [c for c in contracts if 0 <= c.days_until_renewal <= 90]
    expired = Contract.objects.filter(
        client=client, status="active", renewal_date__lt=today
    ).count()
    total_annual = sum(c.annual_cost for c in contracts)
    urgent_contracts = [c for c in contracts if c.days_until_renewal <= 30]

    return render(request, "contracts/list.html", {
        "active_page":        "contracts",
        "client":             client,
        "contracts":          contracts,
        "expiring_soon":      expiring_soon,
        "expired":            expired,
        "total_annual_cost":  total_annual,
        "total_monthly_cost": (total_annual / 12) if total_annual else 0,
        "has_urgent":         len(urgent_contracts) > 0,
        "urgent_count":       len(urgent_contracts),
    })


@login_required
@require_client_access
def contract_detail(request, slug, pk):
    client = get_object_or_404(Client, slug=slug)
    contract = get_object_or_404(Contract, pk=pk, client=client)
    renewals = contract.renewals.order_by("-renewed_at")

    return render(request, "contracts/detail.html", {
        "active_page": "contracts",
        "client":      client,
        "contract":    contract,
        "renewals":    renewals,
    })


@login_required
@require_client_access
def contract_form(request, slug, pk=None):
    """Handles both create (pk=None) and edit (pk provided)."""
    client = get_object_or_404(Client, slug=slug)
    contract = get_object_or_404(Contract, pk=pk, client=client) if pk else None

    form_data = {f: "" for f in CONTRACT_FORM_FIELDS}
    form_data["currency"] = "CAD"
    form_data["auto_renews"] = True
    errors = {}

    if contract is not None:
        form_data.update({
            "vendor_name":    contract.vendor_name,
            "product_name":   contract.product_name,
            "annual_cost":    contract.annual_cost,
            "currency":       contract.currency,
            "renewal_date":   contract.renewal_date.isoformat(),
            "owner_name":     contract.owner_name,
            "owner_email":    contract.owner_email,
            "payment_method": contract.payment_method,
            "notes":          contract.notes,
        })
        form_data["auto_renews"] = contract.auto_renews

    if request.method == "POST":
        for key in CONTRACT_FORM_FIELDS:
            form_data[key] = request.POST.get(key, "").strip()
        form_data["auto_renews"] = request.POST.get("auto_renews") == "on"

        if not form_data["vendor_name"]:
            errors["vendor_name"] = "Vendor name is required."
        if not form_data["product_name"]:
            errors["product_name"] = "Product name is required."

        annual_cost = None
        try:
            annual_cost = Decimal(form_data["annual_cost"])
            if annual_cost < 0:
                errors["annual_cost"] = "Annual cost cannot be negative."
        except (InvalidOperation, ValueError):
            errors["annual_cost"] = "Enter a valid amount."

        if form_data["currency"] not in ("CAD", "USD"):
            errors["currency"] = "Choose CAD or USD."

        renewal_date_val = None
        try:
            renewal_date_val = date.fromisoformat(form_data["renewal_date"])
        except (ValueError, TypeError):
            errors["renewal_date"] = "Enter a valid date."

        if not form_data["owner_name"]:
            errors["owner_name"] = "Owner name is required."

        owner_email = form_data["owner_email"]
        if not owner_email:
            errors["owner_email"] = "Owner email is required."
        else:
            try:
                validate_email(owner_email)
            except DjangoValidationError:
                errors["owner_email"] = "Enter a valid email address."

        if not errors:
            if contract is None:
                contract = Contract(client=client)
            contract.vendor_name    = form_data["vendor_name"]
            contract.product_name   = form_data["product_name"]
            contract.annual_cost    = annual_cost
            contract.currency       = form_data["currency"]
            contract.renewal_date   = renewal_date_val
            contract.owner_name     = form_data["owner_name"]
            contract.owner_email    = owner_email
            contract.auto_renews    = form_data["auto_renews"]
            contract.payment_method = form_data["payment_method"]
            contract.notes          = form_data["notes"]
            contract.save()
            return redirect("contracts:detail", slug=slug, pk=contract.pk)

    return render(request, "contracts/form.html", {
        "active_page": "contracts",
        "client":      client,
        "contract":    contract,
        "form_data":   form_data,
        "errors":      errors,
    })


@login_required
@require_client_access
@require_POST
def mark_renewed(request, slug, pk):
    client = get_object_or_404(Client, slug=slug)
    contract = get_object_or_404(Contract, pk=pk, client=client)

    new_cost = Decimal(request.POST.get("new_cost", contract.annual_cost))
    notes = request.POST.get("notes", "")

    ContractRenewal.objects.create(
        contract=contract,
        renewed_at=date.today(),
        new_cost=new_cost,
        notes=notes,
        created_by=request.user,
    )
    # Advance renewal date by one year
    contract.renewal_date = contract.renewal_date.replace(
        year=contract.renewal_date.year + 1
    )
    contract.annual_cost = new_cost
    contract.save()

    return redirect("contracts:detail", slug=slug, pk=contract.pk)


@login_required
@require_client_access
def renewal_calendar(request, slug):
    client = get_object_or_404(Client, slug=slug)
    today = date.today()
    upcoming = Contract.objects.filter(
        client=client,
        status="active",
        renewal_date__gte=today,
        renewal_date__lte=today + timedelta(days=90),
    ).order_by("renewal_date")

    this_week  = [c for c in upcoming if c.days_until_renewal <= 7]
    this_month = [c for c in upcoming if 7 < c.days_until_renewal <= 30]
    next_90    = [c for c in upcoming if 30 < c.days_until_renewal <= 90]
    total_at_risk = sum(c.annual_cost for c in upcoming)

    return render(request, "contracts/upcoming.html", {
        "active_page":    "contracts_upcoming",
        "client":         client,
        "this_week":      this_week,
        "this_month":     this_month,
        "next_90":        next_90,
        "total_at_risk":  total_at_risk,
        "upcoming_count": len(upcoming),
    })
