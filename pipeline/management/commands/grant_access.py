from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from core.models import Client, UserClientAccess


class Command(BaseCommand):
    help = "Grant a user access to a client's dashboard data"

    def add_arguments(self, parser):
        parser.add_argument("username", type=str)
        parser.add_argument("client_slug", type=str)
        parser.add_argument(
            "--role",
            type=str,
            choices=["viewer", "admin"],
            default="viewer",
            help="viewer = read-only dashboard, admin = can also trigger pipeline runs",
        )

    def handle(self, *args, **options):
        username = options["username"]
        slug = options["client_slug"]
        role = options["role"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user found with username '{username}'")

        try:
            client = Client.objects.get(slug=slug)
        except Client.DoesNotExist:
            raise CommandError(f"No client found with slug '{slug}'")

        access, created = UserClientAccess.objects.update_or_create(
            user=user, client=client,
            defaults={"role": role},
        )

        verb = "Granted" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {user.username} → {client.display_name} ({role})"
        ))
