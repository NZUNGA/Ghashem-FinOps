from django.core.management.base import BaseCommand
from pipeline.runner import run_pipeline
from core.config import load_config


class Command(BaseCommand):
    help = "Run the Ghashem FinOps pipeline for one or all active clients"

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            type=str,
            default=None,
            help="Client slug (omit to run all active clients)",
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
            self.stdout.write(f"\n→ Running pipeline for: {client_cfg.display_name}")
            run_obj = run_pipeline(
                client_slug=client_cfg.slug,
                triggered_by="management_command",
            )
            status_symbol = "✓" if run_obj.status == "completed" else "✗"
            self.stdout.write(f"  {status_symbol} {run_obj.status.upper()}")
            if run_obj.error:
                self.stderr.write(f"  Error: {run_obj.error}")
