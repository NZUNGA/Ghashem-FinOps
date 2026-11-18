"""
Upserts Client records from config/clients.yaml.

Deliberately reads the raw YAML dicts rather than going through
core.config.load_config()/ClientConfig — those dataclasses require
subscriptions/focus_areas/report_currency to be present (KeyError
otherwise) and have no notion of contact_name/contact_email, which
this command needs to read optionally. tenant_id, subscription IDs,
and secrets are NOT read or stored here — those stay in Key Vault by
design (see CLAUDE.md architecture); this command only touches the
Client fields that are safe to keep in Postgres.
"""

from datetime import date, timedelta

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Client, Subscription


class Command(BaseCommand):
    help = "Upserts Client records from config/clients.yaml, keyed on slug"

    def handle(self, *args, **options):
        path = settings.CONFIG_DIR / "clients.yaml"

        # encoding="utf-8-sig" strips a leading UTF-8 BOM if present and
        # behaves identically to plain utf-8 otherwise — same approach
        # core/config.py's load_config() uses for the same file.
        with open(path, encoding="utf-8-sig") as f:
            raw = yaml.safe_load(f) or {}

        clients_raw = raw.get("clients") or []
        if not clients_raw:
            self.stdout.write("No clients in YAML, nothing to import")
            return

        count = 0
        for c in clients_raw:
            analysis_start = (
                date.fromisoformat(c["analysis_start"]) if c.get("analysis_start")
                else date.today() - timedelta(days=90)
            )
            analysis_end = (
                date.fromisoformat(c["analysis_end"]) if c.get("analysis_end")
                else date.today()
            )

            client_obj, _ = Client.objects.update_or_create(
                slug=c["slug"],
                defaults={
                    "display_name":      c.get("display_name", c["slug"]),
                    "active":            c.get("active", True),
                    "analysis_start":    analysis_start,
                    "analysis_end":      analysis_end,
                    "report_currency":   c.get("report_currency", "USD"),
                    "contact_name":      c.get("contact_name", ""),
                    "contact_email":     c.get("contact_email", ""),
                    "onboarding_status": "active",
                },
            )

            # Required for core.config.get_client_config_from_db() — a
            # Client with no Subscription row can't run the pipeline via
            # the DB config path at all.
            for sub in c.get("subscriptions", []):
                Subscription.objects.get_or_create(
                    client=client_obj,
                    subscription_id=sub["id"],
                    defaults={"display_name": sub.get("display_name", "Production")},
                )

            count += 1

        self.stdout.write(f"Upserted {count} clients")
