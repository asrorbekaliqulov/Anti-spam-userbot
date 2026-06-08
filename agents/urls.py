from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    # pages
    path("", views.dashboard, name="dashboard"),
    path("logs/", views.logs_page, name="logs"),
    path("userbots/", views.userbots_page, name="userbots"),
    path("userbots/add/", views.qr_add_page, name="qr_add"),
    # json api
    path("api/stats/", views.api_stats, name="api_stats"),
    path("api/logs/", views.api_logs, name="api_logs"),
    path("api/qr/start/", views.api_qr_start, name="api_qr_start"),
    path("api/qr/status/", views.api_qr_status, name="api_qr_status"),
    # actions
    path("userbots/<int:bot_id>/delete/", views.delete_userbot, name="delete_userbot"),
    path("groups/<int:group_id>/toggle/", views.toggle_group, name="toggle_group"),
]
