from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.decorators.csrf import csrf_exempt
from dashboard import views as dashboard_views

urlpatterns = [
    # dashboard.urls is checked FIRST so its admin/clients/ and
    # admin/clients/<slug>/set-tier/ routes (dashboard/urls.py) resolve
    # before path("admin/", admin.site.urls) below would otherwise claim
    # the whole admin/ prefix and swallow them. Any URL dashboard.urls
    # doesn't match (including every real Django-admin path) falls
    # through to the patterns below as normal.
    path("",          include("dashboard.urls")),

    # dashboard.urls's own client/<slug>/... patterns are complete, exact
    # matches (no include()), so they only match a URL that ends exactly
    # there — client/<slug>/contracts/... never matches them and falls
    # through to this include correctly.
    path("client/<slug:slug>/", include("contracts.urls")),

    path("admin/",  admin.site.urls),
    path("login/",  auth_views.LoginView.as_view(template_name="dashboard/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"), name="logout"),

    # SSO — dashboard_views.auth_complete intercepts before social_django
    # (must stay registered before the social_django.urls include below,
    # or Django would never reach it) to handle Microsoft's tenant admin-
    # consent redirect, which arrives here with no OAuth `state` param and
    # otherwise crashes social_django's own complete view. It falls
    # through to social_django's real complete view for normal logins.
    # Still csrf_exempt because Microsoft POSTs back to it after
    # authentication (response_mode=form_post) — social-auth uses the
    # OAuth state parameter for CSRF protection instead of Django's.
    path("auth/complete/<str:backend>/",
         csrf_exempt(dashboard_views.auth_complete),
         name="complete"),
    path("auth/", include("social_django.urls", namespace="social")),

    path("pipeline/", include("pipeline.urls")),
]
