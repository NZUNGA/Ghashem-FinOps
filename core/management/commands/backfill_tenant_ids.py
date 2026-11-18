from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import Client


class Command(BaseCommand):
    help = (
        "Backfills Client.tenant_id from Key Vault for active clients that "
        "were onboarded before the tenant_id DB field existed."
    )

    def handle(self, *args, **options):
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential

        kv = SecretClient(
            vault_url=settings.VAULT_URL,
            credential=DefaultAzureCredential(),
        )

        clients = Client.objects.filter(active=True, tenant_id="")
        if not clients.exists():
            self.stdout.write("No active clients with a missing tenant_id.")
            return

        for client in clients:
            try:
                tenant_id = kv.get_secret(f"client-{client.slug}-tenant-id").value
                if not tenant_id:
                    self.stdout.write(
                        self.style.WARNING(f"{client.slug}: secret empty — skipped")
                    )
                    continue
                client.tenant_id = tenant_id
                client.save(update_fields=["tenant_id"])
                self.stdout.write(self.style.SUCCESS(f"{client.slug}: {tenant_id}"))
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"{client.slug}: failed — {exc}"))
