from django.urls import path
from . import views

app_name = "contracts"

urlpatterns = [
    path("contracts/",              views.contract_list,     name="list"),
    path("contracts/add/",          views.contract_form,     name="create"),
    path("contracts/upcoming/",     views.renewal_calendar,  name="upcoming"),
    path("contracts/<int:pk>/",         views.contract_detail, name="detail"),
    path("contracts/<int:pk>/edit/",    views.contract_form,   name="edit"),
    path("contracts/<int:pk>/renew/",   views.mark_renewed,    name="renew"),
]
