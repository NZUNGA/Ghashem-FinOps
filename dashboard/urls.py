from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("",                                     views.home,          name="home"),
    path("health/",                              views.health,        name="health"),
    path("client/<slug:slug>/",                  views.summary,       name="summary"),
    path("client/<slug:slug>/resources/",        views.all_resources, name="all_resources"),
    path("client/<slug:slug>/resource-history/", views.resource_history, name="resource_history"),
    path("client/<slug:slug>/findings/",         views.findings,      name="findings"),
    path("client/<slug:slug>/findings/<int:finding_id>/resolve/",
         views.resolve_finding, name="resolve_finding"),
    path("client/<slug:slug>/ai-governance/",
         views.ai_governance_page, name="ai_governance_page"),
    path("client/<slug:slug>/deep-dive/",        views.deep_dive,     name="deep_dive"),
    path("client/<slug:slug>/metrics/",          views.metrics_deep_dive, name="metrics"),
    path("client/<slug:slug>/spend/",            views.spend,         name="spend"),
    path("client/<slug:slug>/brief/download/",   views.download_brief, name="download_brief"),

    # Client onboarding (staff-only — see core.access.staff_member_required)
    path("onboard/new/",                          views.onboard_new,        name="onboard_new"),
    path("onboard/<slug:slug>/next-steps/",       views.onboard_next_steps, name="onboard_next_steps"),

    # Client management (staff-only). Registered under admin/ here rather
    # than colliding with django.contrib.admin's own admin/ prefix — see
    # the include() ordering note in ghashem/urls.py.
    path("admin/clients/",                        views.client_list, name="client_list"),
    path("admin/clients/<slug:slug>/set-tier/",   views.set_tier,     name="set_tier"),
    path("admin/clients/<slug:slug>/delete/",     views.delete_client, name="delete_client"),

    # AJAX endpoints for charts
    path("client/<slug:slug>/api/spend-chart/",  views.api_spend_chart,   name="api_spend_chart"),
    path("client/<slug:slug>/api/metrics/<str:resource_name>/",
         views.api_resource_metrics, name="api_resource_metrics"),
]
