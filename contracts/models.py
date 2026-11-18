from datetime import date
from django.db import models
from django.contrib.auth.models import User
from core.models import Client


class Contract(models.Model):
    client          = models.ForeignKey(Client,
                        on_delete=models.CASCADE,
                        related_name="contracts")
    vendor_name     = models.CharField(max_length=200)
    product_name    = models.CharField(max_length=200)
    annual_cost     = models.DecimalField(
                        max_digits=12, decimal_places=2)
    currency        = models.CharField(max_length=3, default="CAD")
    renewal_date    = models.DateField()
    owner_name      = models.CharField(max_length=200)
    owner_email     = models.EmailField()
    auto_renews     = models.BooleanField(default=True)
    payment_method  = models.CharField(max_length=100, blank=True)
    notes           = models.TextField(blank=True)
    status          = models.CharField(
                        max_length=20,
                        choices=[
                          ("active",    "Active"),
                          ("cancelled", "Cancelled"),
                          ("expired",   "Expired"),
                        ],
                        default="active")
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["renewal_date"]

    def __str__(self):
        return f"{self.vendor_name} — {self.product_name}"

    @property
    def monthly_cost(self):
        return self.annual_cost / 12

    @property
    def days_until_renewal(self):
        return (self.renewal_date - date.today()).days

    @property
    def renewal_urgency(self):
        days = self.days_until_renewal
        if days < 0:   return "expired"
        if days <= 30: return "urgent"
        if days <= 90: return "soon"
        return "ok"


class ContractRenewal(models.Model):
    contract    = models.ForeignKey(Contract,
                    on_delete=models.CASCADE,
                    related_name="renewals")
    renewed_at  = models.DateField()
    new_cost    = models.DecimalField(
                    max_digits=12, decimal_places=2)
    notes       = models.TextField(blank=True)
    created_by  = models.ForeignKey(User,
                    on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.contract} renewed {self.renewed_at}"
