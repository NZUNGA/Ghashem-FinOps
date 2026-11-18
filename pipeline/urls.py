from django.urls import path
from . import views

app_name = "pipeline"

urlpatterns = [
    path("trigger/<slug:slug>/",     views.trigger_pipeline,    name="trigger"),
    path("live-check/<slug:slug>/",  views.trigger_live_check,  name="live_check"),
    path("status/<int:run_id>/",     views.run_status,          name="status"),
]
