# FOCUS-aligned DailyCost columns — see CLAUDE.md.
# Hand-written (not machine-generated) so the two renames are guaranteed
# to be RenameField operations rather than makemigrations' non-interactive
# fallback of drop+add, which would silently discard every existing
# DailyCost row's cost/currency data instead of preserving it.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_client_contact_name_client_onboarding_status'),
    ]

    operations = [
        migrations.RenameField(
            model_name='dailycost',
            old_name='cost',
            new_name='billed_cost',
        ),
        migrations.RenameField(
            model_name='dailycost',
            old_name='currency',
            new_name='billing_currency',
        ),
        migrations.AddField(
            model_name='dailycost',
            name='effective_cost',
            field=models.DecimalField(
                decimal_places=6, default=0, max_digits=14,
                help_text='Cost after commitment discounts '
                          '(reservations, savings plans)',
            ),
        ),
        migrations.AddField(
            model_name='dailycost',
            name='service_category',
            field=models.CharField(
                blank=True, default='', max_length=100,
                help_text='FOCUS ServiceCategory: Compute, Storage, '
                          'Networking, Database, etc.',
            ),
        ),
        migrations.AddField(
            model_name='dailycost',
            name='charge_type',
            field=models.CharField(
                blank=True, default='', max_length=50,
                help_text='FOCUS ChargeCategory: Usage, Purchase, '
                          'Tax, Credit, Adjustment',
            ),
        ),
        migrations.AddField(
            model_name='dailycost',
            name='provider',
            field=models.CharField(
                default='Microsoft', max_length=50,
                help_text='FOCUS ProviderName',
            ),
        ),
        migrations.AddField(
            model_name='dailycost',
            name='pricing_category',
            field=models.CharField(
                blank=True, default='', max_length=50,
                help_text='On-Demand, Commitment-Based, Spot/Interruptible',
            ),
        ),
    ]
