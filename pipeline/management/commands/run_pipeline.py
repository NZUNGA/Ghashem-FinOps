from django.core.management.base import BaseCommand
from pipeline.runner import run_pipeline
from core.config import load_config, get_client_config_from_db


class Command(BaseCommand):
    help = "Run the Ghashem FinOps pipeline for one or all active clients"

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            type=str,
            default=None,
            help="Client slug to run (omit to run all active clients)",
        )

    def handle(self, *args, **options):
        slug = options.get("client")

        if slug:
            # DB-first, same fallback pattern as pipeline/runner.py — a
            # client that only exists in the DB (no clients.yaml entry)
            # can be targeted directly by slug here without needing a
            # YAML entry at all.
            try:
                targets = [get_client_config_from_db(slug)]
            except ValueError:
                config = load_config()
                targets = [config.get_client(slug)]
        else:
            # "Run all active clients" with no --client flag is still
            # YAML-only for now — there's no DB equivalent of
            # active_clients() wired up yet, so a client that exists
            # only in the DB won't be picked up by a bare run with no
            # --client flag. Use --client <slug> to run a DB-only client.
            config = load_config()
            targets = config.active_clients()

        for client_cfg in targets:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n→ Running pipeline for: {client_cfg.display_name}"
            ))
            run_obj = run_pipeline(
                client_slug=client_cfg.slug,
                triggered_by="management_command",
            )
            if run_obj.status == "completed":
                self.stdout.write(self.style.SUCCESS(f"  ✓ Completed"))
            elif run_obj.status == "completed_with_errors":
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ Completed with errors: {run_obj.error}"
                ))
            else:
                self.stderr.write(self.style.ERROR(
                    f"  ✗ Failed: {run_obj.error}"
                ))
