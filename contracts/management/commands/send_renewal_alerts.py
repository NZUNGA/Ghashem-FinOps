from datetime import date, timedelta
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send renewal alerts for contracts due in 90/30/7 days"

    def handle(self, *args, **options):
        from contracts.models import Contract
        from core.email import send_renewal_alert

        today = date.today()
        sent = 0
        skipped = 0

        for days in [90, 30, 7]:
            target_date = today + timedelta(days=days)
            contracts = Contract.objects.filter(
                status="active",
                renewal_date=target_date
            ).select_related("client")

            for contract in contracts:
                if send_renewal_alert(contract, days):
                    sent += 1
                    self.stdout.write(
                        f"  Sent {days}-day alert: "
                        f"{contract.vendor_name} → "
                        f"{contract.owner_email}"
                    )
                else:
                    skipped += 1

        self.stdout.write(
            f"Done. Sent: {sent}, Failed: {skipped}"
        )
