from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    # pages
    path("", views.dashboard, name="dashboard"),
    path("logs/", views.logs_page, name="logs"),
    path("userbots/", views.userbots_page, name="userbots"),
    path("userbots/add/", views.qr_add_page, name="qr_add"),
    path("filters/", views.filters_page, name="filters"),
    path("propagation/", views.propagation_page, name="propagation"),
    # json api
    path("api/stats/", views.api_stats, name="api_stats"),
    path("api/logs/", views.api_logs, name="api_logs"),
    path("api/jobs/", views.api_jobs, name="api_jobs"),
    path("api/qr/start/", views.api_qr_start, name="api_qr_start"),
    path("api/qr/status/", views.api_qr_status, name="api_qr_status"),
    path("api/qr/password/", views.api_qr_password, name="api_qr_password"),
    # actions
    path("userbots/<int:bot_id>/delete/", views.delete_userbot, name="delete_userbot"),
    path("groups/<int:group_id>/toggle/", views.toggle_group, name="toggle_group"),
    path("filters/add/", views.add_rule, name="add_rule"),
    path("filters/<int:rule_id>/toggle/", views.toggle_rule, name="toggle_rule"),
    path("filters/<int:rule_id>/delete/", views.delete_rule, name="delete_rule"),
    path("propagation/create/", views.create_propagation, name="create_propagation"),
]
