from django.core.management.base import BaseCommand
from core.config import load_config
from core.auth import load_client_context
from pipeline.live_check import run_live_check


class Command(BaseCommand):
    help = ("Runs a lightweight live-existence check on all known resources — "
            "marks resources as deleted (and resolves their open findings) if "
            "they no longer exist in Azure. Much faster than the full pipeline; "
            "safe to run frequently.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            type=str,
            default=None,
            help="Client slug to check (omit to check all active clients)",
        )

    def handle(self, *args, **options):
        config = load_config()
        slug = options.get("client")

        targets = (
            [config.get_client(slug)]
            if slug
            else config.active_clients()
        )

        for client_cfg in targets:
            self.stdout.write(f"\n→ Live check: {client_cfg.display_name}")

            from core.models import Client
            try:
                client_obj = Client.objects.get(slug=client_cfg.slug)
            except Client.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  No Client record yet — run the full pipeline first."
                ))
                continue

            subscription_ids = [s.id for s in client_cfg.subscriptions]
            ctx = load_client_context(
                client_slug=client_cfg.slug,
                app_client_id=config.app_client_id,
                vault_url=config.vault_url,
                subscription_id=subscription_ids[0],
            )

            result = run_live_check(client_obj, ctx, subscription_ids)

            if result.get("error"):
                self.stderr.write(self.style.ERROR(f"  Query failed: {result['error']}"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  Checked {result['checked']} resources — "
                    f"{result['deleted']} no longer exist, "
                    f"{result['still_exists']} still exist, "
                    f"{result.get('skipped', 0)} skipped (have open findings)"
                ))
