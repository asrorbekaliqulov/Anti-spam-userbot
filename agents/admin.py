from django.contrib import admin

from .models import (
    BlacklistUser,
    ChatMessage,
    EngineCommand,
    FilterRule,
    GroupMessage,
    PropagationJob,
    SecurityLog,
    SpamContent,
    TelegramDialog,
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



@admin.register(FilterRule)
class FilterRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "rule_type", "pattern", "is_active", "hits", "created_at")
    list_filter = ("rule_type", "is_active")
    search_fields = ("pattern", "note")


@admin.register(PropagationJob)
class PropagationJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "admin_bot", "new_bot", "chat_id", "status", "result", "created_at"
    )
    list_filter = ("status",)



@admin.register(TelegramDialog)
class TelegramDialogAdmin(admin.ModelAdmin):
    list_display = (
        "id", "userbot", "dialog_type", "title", "username",
        "is_admin", "monitored", "members_count", "synced_at",
    )
    list_filter = ("dialog_type", "is_admin", "monitored", "userbot")
    search_fields = ("title", "username", "chat_id")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id", "userbot", "chat_id", "message_id",
        "sender_name", "media_type", "date",
    )
    list_filter = ("userbot", "media_type")
    search_fields = ("text", "sender_name", "sender_username", "chat_id")


@admin.register(EngineCommand)
class EngineCommandAdmin(admin.ModelAdmin):
    list_display = ("id", "userbot", "kind", "chat_id", "status", "result", "created_at")
    list_filter = ("kind", "status")



@admin.register(GroupMessage)
class GroupMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id", "userbot", "chat_id", "message_id",
        "sender_role", "sender_name", "tg_scam_flag", "date",
    )
    list_filter = ("sender_role", "tg_scam_flag", "userbot")
    search_fields = ("text", "sender_name", "sender_username", "sender_id", "chat_id")
    date_hierarchy = "date"
