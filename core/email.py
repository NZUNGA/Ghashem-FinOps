"""
Client-facing email alerts — cost anomaly spikes and the monthly
executive brief.

Thin wrapper around Django's email backend — console backend in dev
(see EMAIL_BACKEND in settings.py), a real backend in production. Must
never raise: a broken mail relay can't be allowed to fail a pipeline
run that otherwise completed successfully.
"""

import logging
import secrets
from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.utils import timezone
from core.models import Client, Finding, PipelineRun
from core.utils import format_capped_pct

log = logging.getLogger(__name__)


def send_anomaly_alert(client: Client, finding: Finding) -> bool:
    """
    Sends client.contact_email an HTML cost-spike alert for the given
    cost_anomaly Finding. Returns True if the email was handed off to
    the backend, False if it was skipped or failed — callers should
    treat both as non-fatal to the pipeline.
    """
    if not client.contact_email:
        log.warning(
            f"[{client.slug}] Cost anomaly detected but no contact_email "
            f"on file — skipping alert email"
        )
        return False

    try:
        contact_name  = getattr(settings, 'GHASHEM_CONTACT_NAME', 'Ghashem')
        contact_email = getattr(settings, 'GHASHEM_CONTACT_EMAIL', 'support@ghashem.ca')
        site_url      = settings.SITE_URL
        from_email    = settings.DEFAULT_FROM_EMAIL

        extra = finding.extra_data or {}
        pct_over      = extra.get("pct_over_baseline", 0)
        pct_display   = format_capped_pct(pct_over)
        top_resources = extra.get("top_spike_resources", [])
        dashboard_url = f"{site_url.rstrip('/')}/client/{client.slug}/"

        subject = f"⚠ Azure cost spike detected — {client.display_name}"

        if top_resources:
            rows_html = "".join(
                "<tr>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #E4E2D9'>{r['name']}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #E4E2D9'>${r['normal_weekly_cost']:.2f}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #E4E2D9'>${r['this_week_cost']:.2f}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #E4E2D9;color:#B86A20'>+${r['delta']:.2f}</td>"
                "</tr>"
                for r in top_resources
            )
        else:
            rows_html = (
                "<tr><td colspan='4' style='padding:6px 10px'>"
                "No single resource stands out — the increase is spread "
                "across your spend.</td></tr>"
            )

        html_body = f"""
        <div style="font-family:sans-serif;color:#1A2332;max-width:600px">
          <p>Azure spend for <strong>{client.display_name}</strong> is running
          {pct_display} above the normal weekly baseline this week.</p>

          <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <thead>
              <tr style="text-align:left;font-size:12px;color:#8B92A3">
                <th style="padding:6px 10px">Resource</th>
                <th style="padding:6px 10px">Normal cost</th>
                <th style="padding:6px 10px">This week</th>
                <th style="padding:6px 10px">Difference</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>

          <p><strong>Recommended action:</strong> {finding.action}</p>

          <p><a href="{dashboard_url}">View the full findings on your dashboard →</a></p>

          <p style="margin-top:24px;color:#4B5468">{contact_name}<br>Ghashem</p>
        </div>
        """

        text_body = (
            f"Azure spend for {client.display_name} is running {pct_display} "
            f"above the normal weekly baseline this week.\n\n"
            f"{finding.evidence}\n\n"
            f"Recommended action: {finding.action}\n\n"
            f"Dashboard: {dashboard_url}\n\n"
            f"{contact_name}, Ghashem"
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[client.contact_email],
            reply_to=[contact_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        log.warning(f"[{client.slug}] Failed to send cost anomaly alert: {exc}")
        return False


def send_monthly_brief(client: Client, pdf_bytes: bytes, run_obj: PipelineRun) -> bool:
    """
    Emails client.contact_email the monthly executive brief PDF
    (see core/brief.py). Returns True if the email was handed off to
    the backend, False if it was skipped or failed — callers should
    treat both as non-fatal to the pipeline, same as send_anomaly_alert.
    """
    if not client.contact_email:
        log.warning(
            f"[{client.slug}] Monthly brief generated but no contact_email "
            f"on file — skipping brief email"
        )
        return False

    try:
        from core.brief import compute_brief_stats

        contact_name  = getattr(settings, 'GHASHEM_CONTACT_NAME', 'Ghashem')
        contact_email = getattr(settings, 'GHASHEM_CONTACT_EMAIL', 'support@ghashem.ca')
        site_url      = settings.SITE_URL
        from_email    = settings.DEFAULT_FROM_EMAIL

        today = timezone.localdate()
        month_name, year = today.strftime("%B"), today.year
        stats = compute_brief_stats(client)
        dashboard_url = f"{site_url.rstrip('/')}/client/{client.slug}/"

        subject = f"Your Azure cost brief — {month_name} {year} · {client.display_name}"

        text_body = (
            f"Hi,\n\n"
            f"Your monthly Azure cost brief for {client.display_name} is attached.\n\n"
            f"Total spend this period: ${stats['total_spend']:,.2f} {client.report_currency}\n"
            f"Identified waste: ${stats['total_savings']:,.2f}/mo\n"
            f"Open findings: {stats['open_findings_count']}\n\n"
            f"Full details: {dashboard_url}\n\n"
            f"{contact_name}, Ghashem"
        )

        filename = f"Ghashem_{client.slug}_{today.strftime('%Y_%m')}.pdf"

        msg = EmailMessage(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[client.contact_email],
            reply_to=[contact_email],
        )
        msg.attach(filename, pdf_bytes, "application/pdf")
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        log.error(f"[{client.slug}] Failed to send monthly brief: {exc}")
        return False


def send_welcome_email(client: Client, run_summary: dict) -> bool:
    """
    Sends the client's first-ever welcome email, right after their first
    completed pipeline run (see pipeline/runner.py). Auto-provisions a
    dashboard login (Django User + viewer UserClientAccess grant) for
    client.contact_email if one doesn't already exist, and includes the
    temporary password in the email only when a new account was just
    created — an existing account's password is never touched or shown.

    run_summary is expected to have the same shape as
    core.brief.compute_brief_stats(): total_spend, total_savings,
    annual_savings, open_findings_count (kept in one place there so this
    and the monthly brief can't report different numbers for the same
    client).

    Returns True if the email was handed off to the backend, False if
    skipped or failed — callers should treat both as non-fatal to the
    pipeline, same as the other core/email.py senders.
    """
    if not client.contact_email:
        log.warning(
            f"[{client.slug}] First pipeline run completed but no "
            f"contact_email on file — skipping welcome email"
        )
        return False

    try:
        from django.contrib.auth.models import User
        from core.models import UserClientAccess

        temp_password = None
        user = User.objects.filter(email=client.contact_email).first()
        if user is None:
            temp_password = secrets.token_urlsafe(9)
            user = User.objects.create_user(
                username=client.contact_email,
                email=client.contact_email,
                password=temp_password,
            )
            user.is_active = True
            user.save(update_fields=["is_active"])
            UserClientAccess.objects.get_or_create(
                user=user, client=client, defaults={"role": "viewer"},
            )
        else:
            # Safety net against a stale contact_email collision (manual DB
            # edit, YAML-onboarded client, data migrated in) — onboard_new
            # blocks duplicate contact emails at creation time, but
            # auth.User.email has no uniqueness constraint, so this is what
            # actually stops a silent cross-client UserClientAccess grant if
            # that check is ever bypassed. A user who already has access to
            # THIS same client (e.g. a re-run) falls through normally below.
            existing_clients = UserClientAccess.objects.filter(
                user=user
            ).exclude(client=client).values_list("client__display_name", flat=True)
            if existing_clients.exists():
                log.warning(
                    "send_welcome_email: email %s already has access to other "
                    "clients %s — skipping auto-grant for client %s to prevent "
                    "cross-client access",
                    client.contact_email,
                    list(existing_clients),
                    client.slug,
                )
                return False

        contact_name  = getattr(settings, 'GHASHEM_CONTACT_NAME', 'Ghashem')
        contact_email = getattr(settings, 'GHASHEM_CONTACT_EMAIL', 'support@ghashem.ca')
        site_url      = settings.SITE_URL.rstrip("/")
        from_email    = settings.DEFAULT_FROM_EMAIL
        dashboard_url = f"{site_url}/client/{client.slug}/"
        greeting_name = client.contact_name or "there"

        subject = f"Your Azure cost analysis is ready — {client.display_name}"

        total_findings  = run_summary["open_findings_count"]
        total_savings   = run_summary["total_savings"]
        annual_savings  = run_summary["annual_savings"]
        total_spend     = run_summary["total_spend"]

        if temp_password:
            credentials_html = f"""
            <p style="border-top:1px solid #E4E2D9;border-bottom:1px solid #E4E2D9;padding:12px 0">
              Your login credentials:<br>
              URL: <a href="{site_url}">{site_url}</a><br>
              Username: {client.contact_email}<br>
              Temporary password: <strong>{temp_password}</strong><br>
              (You'll be prompted to change this on first login)
            </p>
            """
            credentials_text = (
                f"---\n"
                f"Your login credentials:\n"
                f"URL: {site_url}\n"
                f"Username: {client.contact_email}\n"
                f"Temporary password: {temp_password}\n"
                f"(You'll be prompted to change this on first login)\n"
                f"---\n\n"
            )
        else:
            credentials_html = ""
            credentials_text = ""

        html_body = f"""
        <div style="font-family:sans-serif;color:#1A2332;max-width:600px">
          <p>Hi {greeting_name},</p>

          <p>Your first Azure cost analysis is complete.</p>

          <p>Here's what we found:</p>
          <ul>
            <li>{total_findings} cost optimization opportunities</li>
            <li>${total_savings:,.2f}/month in identified waste</li>
            <li>${annual_savings:,.2f}/year in potential savings</li>
            <li>${total_spend:,.2f} analyzed over the last 90 days</li>
          </ul>

          <p><a href="{dashboard_url}">View your dashboard →</a></p>

          {credentials_html}

          <p>Let's schedule a 30-minute call to walk through the findings together.
          Reply to this email to book a time.</p>

          <p style="margin-top:24px;color:#4B5468">
            {contact_name}<br>{contact_email}<br>Ghashem · Azure FinOps Intelligence
          </p>
        </div>
        """

        text_body = (
            f"Hi {greeting_name},\n\n"
            f"Your first Azure cost analysis is complete.\n\n"
            f"Here's what we found:\n"
            f"• {total_findings} cost optimization opportunities\n"
            f"• ${total_savings:,.2f}/month in identified waste\n"
            f"• ${annual_savings:,.2f}/year in potential savings\n"
            f"• ${total_spend:,.2f} analyzed over the last 90 days\n\n"
            f"View your dashboard → {dashboard_url}\n\n"
            f"{credentials_text}"
            f"Let's schedule a 30-minute call to walk through the findings together. "
            f"Reply to this email to book a time.\n\n"
            f"{contact_name}\n{contact_email}\n"
            f"Ghashem · Azure FinOps Intelligence"
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[client.contact_email],
            reply_to=[contact_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        log.error(f"[{client.slug}] Failed to send welcome email: {exc}")
        return False


def send_renewal_alert(contract, days_until):
    """
    Sends renewal reminder to contract.owner_email.
    Called by send_renewal_alerts management command.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    subject = (
        f"Renewal reminder: {contract.vendor_name} "
        f"{contract.product_name} renews in {days_until} days "
        f"— ${contract.annual_cost:,.0f}/year"
    )

    auto_renew_note = (
        f"This contract is set to auto-renew. If you want to "
        f"cancel or renegotiate, act before "
        f"{contract.renewal_date.strftime('%B %d, %Y')}."
        if contract.auto_renews else
        f"This contract does NOT auto-renew. You will need to "
        f"manually renew it before "
        f"{contract.renewal_date.strftime('%B %d, %Y')} "
        f"to avoid interruption."
    )

    site_url = getattr(settings, "SITE_URL", "")
    review_url = (
        f"{site_url}/client/{contract.client.slug}"
        f"/contracts/{contract.pk}/"
    )

    body = (
        f"Hi {contract.owner_name},\n\n"
        f"This is a reminder that your "
        f"{contract.vendor_name} {contract.product_name} "
        f"contract renews on "
        f"{contract.renewal_date.strftime('%B %d, %Y')} "
        f"— in {days_until} days.\n\n"
        f"Annual cost: ${contract.annual_cost:,.2f} "
        f"{contract.currency}\n"
        f"Auto-renews: {'Yes' if contract.auto_renews else 'No'}\n\n"
        f"{auto_renew_note}\n\n"
        f"Review this contract: {review_url}\n\n"
        f"{getattr(settings, 'GHASHEM_CONTACT_NAME', 'Ghashem')}\n"
        f"Ghashem"
    )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(
                settings, "DEFAULT_FROM_EMAIL",
                "noreply@ghashem.com"),
            recipient_list=[contract.owner_email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to send renewal alert for "
            f"contract {contract.pk}: {e}"
        )
        return False
