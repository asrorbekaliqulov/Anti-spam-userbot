from django.contrib import admin

from .models import (
    BlacklistUser,
    SecurityLog,
    SpamContent,
    TelegramGroup,
    UserBot,
)


@admin.register(UserBot)
class UserBotAdmin(admin.ModelAdmin):
    list_display = ("id", "username", "phone", "status", "telegram_id", "created_at")
    list_filter = ("status",)
    search_fields = ("username", "phone", "telegram_id")
    readonly_fields = ("session_string", "created_at", "updated_at")


@admin.register(TelegramGroup)
class TelegramGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "chat_id", "monitored_by", "is_active")
    list_filter = ("is_active", "monitored_by")
    search_fields = ("title", "chat_id")


@admin.register(BlacklistUser)
class BlacklistUserAdmin(admin.ModelAdmin):
    list_display = ("telegram_id", "username", "first_name", "reason_text", "detected_at")
    search_fields = ("telegram_id", "username", "first_name")
    list_filter = ("detected_at",)


@admin.register(SpamContent)
class SpamContentAdmin(admin.ModelAdmin):
    list_display = ("id", "content_type", "content_hash", "hits", "added_at")
    list_filter = ("content_type",)
    search_fields = ("content_hash", "raw_data")


@admin.register(SecurityLog)
class SecurityLogAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "action_taken",
        "stage",
        "spammer_id",
        "spammer_username",
        "group",
        "handled_by",
    )
    list_filter = ("action_taken", "stage", "group")
    search_fields = ("spammer_id", "spammer_username", "detail")
    date_hierarchy = "timestamp"
