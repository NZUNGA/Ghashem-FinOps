from django.contrib import admin
from .models import (
    Client, Subscription, PipelineRun, Finding, DailyMetric, DailyCost,
    UserClientAccess, AccessLog, ResourceStatus,
)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display  = ["display_name", "slug", "active", "analysis_start", "analysis_end"]
    list_filter   = ["active"]
    search_fields = ["display_name", "slug"]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ["client", "display_name", "subscription_id"]


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display  = ["client", "status", "started_at", "finished_at", "triggered_by"]
    list_filter   = ["status", "client"]
    readonly_fields = ["log", "error"]


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    list_display  = ["resource_name", "finding_type", "severity",
                     "estimated_monthly_savings_usd", "client"]
    list_filter   = ["severity", "finding_type", "client"]
    search_fields = ["resource_name", "resource_id"]


@admin.register(UserClientAccess)
class UserClientAccessAdmin(admin.ModelAdmin):
    list_display    = ["user", "client", "role", "granted_by", "granted_at"]
    list_filter     = ["role", "client"]
    search_fields   = ["user__username", "client__display_name"]
    autocomplete_fields = ["user", "client"]

    def save_model(self, request, obj, form, change):
        if not change:  # only set granted_by on creation
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(AccessLog)
class AccessLogAdmin(admin.ModelAdmin):
    list_display  = ["timestamp", "user", "client_slug", "path", "allowed", "ip_address"]
    list_filter   = ["allowed", "client_slug"]
    search_fields = ["user__username", "client_slug", "ip_address"]
    readonly_fields = [f.name for f in AccessLog._meta.fields]

    def has_add_permission(self, request):
        return False  # logs are system-generated only, never manually created

    def has_change_permission(self, request, obj=None):
        return False  # logs are immutable — a pentest checks this specifically


@admin.register(ResourceStatus)
class ResourceStatusAdmin(admin.ModelAdmin):
    list_display  = ["resource_id", "client", "exists", "last_checked_at"]
    list_filter   = ["exists", "client"]
    search_fields = ["resource_id"]


admin.site.register(DailyMetric)
admin.site.register(DailyCost)
