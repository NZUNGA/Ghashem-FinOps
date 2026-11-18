from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings


class Command(BaseCommand):
    help = "Send a test email to verify SendGrid configuration"

    def add_arguments(self, parser):
        parser.add_argument(
            "to_email", type=str, help="Recipient email address"
        )

    def handle(self, *args, **options):
        to_email = options["to_email"]
        backend = settings.EMAIL_BACKEND

        self.stdout.write(f"Backend: {backend}")
        self.stdout.write(f"From: {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"To: {to_email}")
        self.stdout.write("Sending...")

        try:
            send_mail(
                subject="Ghashem — test email configuration",
                message=(
                    "This is a test email from the Ghashem platform.\n\n"
                    "If you received this, SendGrid is configured correctly.\n\n"
                    f"Backend: {backend}"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[to_email],
                fail_silently=False,
            )
            self.stdout.write(self.style.SUCCESS(f"✓ Email sent to {to_email}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ Failed: {e}"))
            raise SystemExit(1)
