from django.db import models
from django.conf import settings
from django.contrib.auth.models import User


class Client(models.Model):
    ONBOARDING_STATUS_CHOICES = [
        ("pending_consent",  "Waiting for admin consent"),
        ("pending_rbac",     "Waiting for RBAC assignment"),
        ("pending_pipeline", "Ready for first pipeline run"),
        ("active",           "Active"),
        ("suspended",        "Suspended"),
    ]

    TIER_CHOICES = [
        ("visibility", "Visibility — 2% of Azure spend, min $299/mo"),
        ("advisory",   "Advisory — 3% of Azure spend, min $599/mo"),
        ("managed",    "Managed — 4% of Azure spend, min $1,200/mo"),
    ]

    slug              = models.SlugField(unique=True)
    display_name      = models.CharField(max_length=200)
    active            = models.BooleanField(default=True)
    analysis_start    = models.DateField(null=True, blank=True)
    analysis_end      = models.DateField(null=True, blank=True)
    report_currency   = models.CharField(max_length=3, default="USD")
    contact_email     = models.EmailField(blank=True, default="")
    contact_name      = models.CharField(max_length=200, blank=True, default="")
    onboarding_status = models.CharField(max_length=20, choices=ONBOARDING_STATUS_CHOICES,
                                         default="active")
    tier = models.CharField(
        max_length=20,
        choices=TIER_CHOICES,
        default="visibility",
        help_text="Service tier determines which features are "
                  "accessible and how the monthly fee is calculated"
    )
    tier_override_monthly = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="If set, overrides the percentage calculation. "
                  "Use for fixed-price deals."
    )
    tenant_id = models.CharField(
        max_length=36,
        blank=True,
        default="",
        db_index=True,
        help_text="Entra tenant ID (UUID). Populated at onboarding; used for SSO routing.",
    )
    created_at        = models.DateTimeField(auto_now_add=True)
    client_sp_object_id = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Service principal object ID of the Ghashem app in "
                  "this client's tenant (differs from the object ID in "
                  "the Ghashem company tenant — a new SP is created per "
                  "tenant on admin consent). Resolved on first successful "
                  "lookup — see core.auth.decode_jwt_oid()."
    )

    def __str__(self):
        return self.display_name

    class Meta:
        ordering = ["display_name"]


class UserClientAccess(models.Model):
    """
    Explicit grant of access from one user to one client.
    This is the core tenant-isolation control: a user can only ever
    view a Client's data if a row exists here. There is no implicit
    access — not even for staff users, unless ROLE_ADMIN is granted
    explicitly per client or the user is a Django superuser.

    This table is intentionally simple (no roles/permissions matrix)
    to keep the MVP auditable — every grant is a deliberate, visible
    row that shows up in the Django admin and in audit reports.
    """
    ROLE_CHOICES = [
        ("viewer", "Viewer — read-only dashboard access"),
        ("admin",  "Admin — can trigger pipeline runs"),
    ]

    user        = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name="client_access")
    client      = models.ForeignKey(Client, on_delete=models.CASCADE,
                                    related_name="user_access")
    role        = models.CharField(max_length=20, choices=ROLE_CHOICES, default="viewer")
    granted_by  = models.ForeignKey(User, on_delete=models.SET_NULL,
                                    null=True, blank=True,
                                    related_name="access_grants_made")
    granted_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "client")
        verbose_name_plural = "User client access grants"

    def __str__(self):
        return f"{self.user.username} → {self.client.slug} ({self.role})"


class AccessLog(models.Model):
    """
    Records every attempt to access client-scoped data, including
    denied attempts. This is what a pentest report — and a real
    security incident investigation — actually needs: a trail showing
    who tried to access what, when, and whether it was allowed.
    """
    user        = models.ForeignKey(User, on_delete=models.SET_NULL,
                                    null=True, related_name="access_logs")
    client_slug = models.CharField(max_length=100)
    path        = models.CharField(max_length=500)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    allowed     = models.BooleanField()
    timestamp   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["client_slug", "timestamp"]),
            models.Index(fields=["allowed", "timestamp"]),
        ]

    def __str__(self):
        status = "ALLOWED" if self.allowed else "DENIED"
        return f"{status}: {self.user} → {self.client_slug} @ {self.timestamp}"


class Subscription(models.Model):
    client       = models.ForeignKey(Client, on_delete=models.CASCADE,
                                     related_name="subscriptions")
    subscription_id  = models.CharField(max_length=100)
    display_name = models.CharField(max_length=200)

    def __str__(self):
        return f"{self.client} — {self.display_name}"


class PipelineRun(models.Model):
    STATUS_CHOICES = [
        ("pending",   "Pending"),
        ("running",   "Running"),
        ("completed", "Completed"),
        ("completed_with_errors", "Completed with errors"),
        ("failed",    "Failed"),
    ]
    client      = models.ForeignKey(Client, on_delete=models.CASCADE,
                                    related_name="pipeline_runs")
    status      = models.CharField(max_length=30, choices=STATUS_CHOICES,
                                   default="pending")
    started_at  = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    log         = models.TextField(blank=True)
    error       = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=100, blank=True)
    current_step = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable label of the current pipeline step, "
                  "updated during execution for real-time progress display"
    )

    def __str__(self):
        return f"{self.client} run {self.pk} ({self.status})"

    class Meta:
        ordering = ["-started_at"]


class Finding(models.Model):
    SEVERITY_CHOICES = [
        ("high",   "High"),
        ("medium", "Medium"),
        ("low",    "Low"),
    ]
    STATUS_CHOICES = [
        ("open",     "Open"),
        ("resolved", "Resolved"),     # resource no longer exists in Azure
        ("stale",    "Needs re-check"),  # live check failed, not yet confirmed either way
    ]
    FINDING_TYPES = [
        ("token_inefficiency", "Token inefficiency"),
        ("idle_account",       "Idle account"),
        ("wrong_tier",         "Wrong tier"),
        ("rate_limiting",      "Rate limiting"),
        ("duplicate_accounts", "Duplicate accounts"),
        ("stale_model",        "Stale custom model"),
        # Zombie / phantom resource findings — detected via Resource Graph
        # and Cost Management only, no Monitor metrics required
        ("unattached_public_ip",  "Unattached public IP"),
        ("orphaned_disk",         "Orphaned managed disk"),
        ("unattached_nic",        "Unattached network interface"),
        ("unused_nsg",            "Unused network security group"),
        ("aging_snapshot",        "Aging snapshot"),
        ("stopped_not_deallocated", "Stopped but not deallocated VM"),
        ("empty_load_balancer",   "Empty load balancer"),
        ("no_autoshutdown",       "No auto-shutdown schedule"),
        # Client-wide (not tied to one Azure resource)
        ("cost_anomaly",          "Cost anomaly"),
        # Advisory — real resource_id, but validity is tied to whether
        # Azure Advisor still recommends it, not whether the resource
        # still exists (see live_check.py's NON_RESOURCE_FINDING_TYPES)
        ("reservation_opportunity", "Reserved Instance opportunity"),
        # AI Cost Intelligence — also advisory, same reasoning as
        # reservation_opportunity above (see live_check.py)
        ("idle_ai_deployment",     "Idle AI model deployment"),
        ("ai_cost_spike",          "AI cost spike detected"),
        ("untagged_ai_resource",   "AI resource missing cost tags"),
        # AI Governance posture — Session 5b (analyzer_ai_posture.py /
        # collector_shadow_ai.py). Same reasoning as the AI Cost
        # Intelligence findings above: real resource_id, but validity is
        # tied to a live configuration/discovery check, not existence —
        # see live_check.py's NON_RESOURCE_FINDING_TYPES.
        ("ai_local_auth_enabled",    "AI resource using API key authentication"),
        ("ai_public_network_access", "AI resource exposed to public internet"),
        ("ai_no_content_filter",     "AI deployment without content filtering"),
        ("ai_no_private_endpoint",   "AI resource without private endpoint"),
        ("shadow_ai_resource",       "Ungoverned AI resource detected"),
    ]

    client                        = models.ForeignKey(Client, on_delete=models.CASCADE,
                                                      related_name="findings")
    pipeline_run                  = models.ForeignKey(PipelineRun, on_delete=models.SET_NULL,
                                                      null=True, related_name="findings")
    resource_id                   = models.TextField()
    resource_name                 = models.CharField(max_length=300)
    resource_group                = models.CharField(max_length=300, blank=True)
    finding_type                  = models.CharField(max_length=50, choices=FINDING_TYPES)
    analyzer                      = models.CharField(max_length=100)
    severity                      = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    estimated_monthly_savings_usd = models.FloatField(default=0)
    evidence                      = models.TextField()
    action                        = models.TextField()
    extra_data                    = models.JSONField(default=dict, blank=True)
    created_at                    = models.DateTimeField(auto_now_add=True)

    # Live-check tracking — set by the existence-check job, not the main pipeline
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    last_checked_at      = models.DateTimeField(null=True, blank=True)
    resolved_at          = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.resource_name} — {self.finding_type} ({self.severity})"

    class Meta:
        ordering = ["-estimated_monthly_savings_usd"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "resource_id", "finding_type"],
                name="unique_finding_per_resource_type",
            )
        ]
        indexes = [
            models.Index(fields=["client", "status"], name="finding_client_status_idx"),
            models.Index(
                fields=["client", "finding_type", "status"],
                name="finding_type_status_idx",
            ),
        ]


class ResourceStatus(models.Model):
    """
    Tracks whether a resource still exists in Azure, independent of
    its DailyMetric history. One row per (client, resource_id) — much
    cheaper to update than flagging every historical metric row, and
    keeps the metric history intact for "this used to cost $X before
    cleanup" reporting even after the resource itself is gone.

    Populated and kept current by pipeline.live_check, and also
    written at the end of every full pipeline run (any resource seen
    in that run's fresh inventory is, by definition, still alive).
    """
    client       = models.ForeignKey(Client, on_delete=models.CASCADE,
                                     related_name="resource_statuses")
    resource_id  = models.TextField()
    exists       = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("client", "resource_id")
        indexes = [
            models.Index(fields=["client", "exists"]),
        ]

    def __str__(self):
        return f"{self.resource_id} — {'exists' if self.exists else 'deleted'}"


class DailyMetric(models.Model):
    client            = models.ForeignKey(Client, on_delete=models.CASCADE,
                                          related_name="metrics")
    resource_id       = models.TextField()
    resource_name     = models.CharField(max_length=300)
    resource_group    = models.CharField(max_length=300, blank=True)
    metric_namespace  = models.CharField(max_length=300)
    metric_name       = models.CharField(max_length=200)
    date              = models.DateField()
    value             = models.FloatField(null=True)
    aggregation       = models.CharField(max_length=50, blank=True)

    class Meta:
        unique_together = ("client", "resource_id", "metric_name", "date")
        indexes = [
            models.Index(fields=["client", "metric_name", "date"]),
            models.Index(fields=["resource_id", "metric_name"]),
        ]


class DailyCost(models.Model):
    """
    FOCUS-aligned column names (FinOps Open Cost and Usage Spec 1.4,
    current as of July 2026) — see CLAUDE.md for the full field
    reference. Do not rename these columns back to Azure-specific names.
    """
    client           = models.ForeignKey(Client, on_delete=models.CASCADE,
                                         related_name="costs")
    date             = models.DateField()
    subscription_id  = models.CharField(max_length=100)
    service_name     = models.CharField(max_length=300)
    resource_group   = models.CharField(max_length=300, blank=True)
    resource_id      = models.TextField(blank=True)
    billed_cost      = models.FloatField(default=0)
    billing_currency = models.CharField(max_length=3, default="USD")
    effective_cost   = models.DecimalField(max_digits=14, decimal_places=6,
                        default=0,
                        help_text="Cost after commitment discounts "
                                  "(reservations, savings plans)")
    service_category = models.CharField(max_length=100, blank=True,
                        default="",
                        help_text="FOCUS ServiceCategory: Compute, "
                                  "Storage, Networking, Database, etc.")
    charge_type      = models.CharField(max_length=50, blank=True,
                        default="",
                        help_text="FOCUS ChargeCategory: Usage, "
                                  "Purchase, Tax, Credit, Adjustment")
    provider         = models.CharField(max_length=50, default="Microsoft",
                        help_text="FOCUS ProviderName")
    pricing_category = models.CharField(max_length=50, blank=True,
                        default="",
                        help_text="On-Demand, Commitment-Based, "
                                  "Spot/Interruptible")

    class Meta:
        indexes = [
            models.Index(fields=["client", "date"]),
            models.Index(fields=["client", "service_name"]),
        ]


class DailyAIUsage(models.Model):
    """
    Azure OpenAI token usage, collected by pipeline/collector_ai.py from
    Azure Monitor metrics. One row per (client, resource, deployment,
    date) — deployment_name is "" when the underlying metric couldn't
    be split by the DeploymentName dimension, so usage is recorded at
    the resource level instead.
    """
    client       = models.ForeignKey(Client, on_delete=models.CASCADE,
                                     related_name="daily_ai_usage")
    pipeline_run = models.ForeignKey(PipelineRun, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="ai_usage")
    resource_id     = models.TextField()
    resource_name    = models.CharField(max_length=255)
    resource_group   = models.CharField(max_length=255, blank=True, default="")
    deployment_name  = models.CharField(max_length=255, blank=True, default="")
    model_name       = models.CharField(max_length=255, blank=True, default="")
    date             = models.DateField()
    input_tokens     = models.BigIntegerField(default=0)
    output_tokens    = models.BigIntegerField(default=0)
    total_tokens     = models.BigIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    class Meta:
        unique_together = ("client", "resource_id", "deployment_name", "date")
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["client", "date"], name="dailyaiusage_client_date_idx"),
        ]

    def __str__(self):
        return (
            f"{self.resource_name}/{self.deployment_name} "
            f"{self.date} — {self.total_tokens} tokens"
        )


class AIResourcePosture(models.Model):
    """
    Security/governance posture snapshot for one AI resource, one row
    per (client, resource_id, pipeline_run) — written by
    pipeline/analyzer_ai_posture.py on every full pipeline run, so
    posture history is preserved across runs rather than overwritten
    in place (unlike DailyAIUsage's per-day upsert).
    """
    client = models.ForeignKey(
        "Client",
        on_delete=models.CASCADE,
        related_name="ai_resource_postures"
    )
    pipeline_run = models.ForeignKey(
        "PipelineRun",
        on_delete=models.SET_NULL,
        null=True, blank=True
    )
    resource_id = models.TextField()
    resource_name = models.CharField(
        max_length=255
    )
    resource_group = models.CharField(
        max_length=255, blank=True, default=""
    )
    resource_type = models.CharField(
        max_length=100, default="openai"
    )
    local_auth_disabled = models.BooleanField(
        null=True, blank=True
    )
    public_network_disabled = models.BooleanField(
        null=True, blank=True
    )
    content_filter_enabled = models.BooleanField(
        null=True, blank=True
    )
    private_endpoint_present = models.BooleanField(
        null=True, blank=True
    )
    defender_enabled = models.BooleanField(
        null=True, blank=True
    )
    required_tags_present = models.BooleanField(
        null=True, blank=True
    )
    posture_score = models.IntegerField(default=0)
    scanned_at = models.DateTimeField(
        auto_now_add=True
    )
    last_seen_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Last time this resource was confirmed to exist in "
                  "Azure via Resource Graph — set by analyzer_ai_posture.py "
                  "on every successful write, read by live_check.py's "
                  "existence check (see ResourceStatus for the actual "
                  "deleted/exists flag)."
    )

    class Meta:
        ordering = ["-scanned_at"]
        unique_together = [
            "client", "resource_id", "pipeline_run"
        ]

    @property
    def passing_controls(self):
        controls = [
            self.local_auth_disabled,
            self.public_network_disabled,
            self.content_filter_enabled,
            self.private_endpoint_present,
            self.defender_enabled,
            self.required_tags_present,
        ]
        return sum(
            1 for c in controls if c is True
        )

    def calculate_score(self):
        self.posture_score = round(
            self.passing_controls / 6 * 10
        )
        return self.posture_score

    def __str__(self):
        return (
            f"{self.resource_name} "
            f"score={self.posture_score}/10"
        )
