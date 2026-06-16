"""Async-safe ORM helpers used by the Telegram engine."""
from __future__ import annotations

from asgiref.sync import sync_to_async
from django.utils import timezone


@sync_to_async
def list_active_userbots() -> list[dict]:
    from agents.models import UserBot

    bots = UserBot.objects.filter(status=UserBot.Status.ACTIVE).exclude(
        session_string=""
    )
    return [
        {
            "id": b.id,
            "session_string": b.session_string,
            "api_id": b.api_id,
            "api_hash": b.api_hash,
            "username": b.username,
            "telegram_id": b.telegram_id,
        }
        for b in bots
    ]


@sync_to_async
def monitored_chat_ids(userbot_id: int) -> list[int]:
    from agents.models import TelegramGroup

    return list(
        TelegramGroup.objects.filter(
            monitored_by_id=userbot_id, is_active=True
        ).values_list("chat_id", flat=True)
    )


@sync_to_async
def set_bot_status(userbot_id: int, status: str) -> None:
    from agents.models import UserBot

    UserBot.objects.filter(id=userbot_id).update(status=status)


@sync_to_async
def upsert_group(userbot_id: int, chat_id: int, title: str, invite_link: str = "") -> int:
    from agents.models import TelegramGroup

    obj, _ = TelegramGroup.objects.update_or_create(
        chat_id=chat_id,
        monitored_by_id=userbot_id,
        defaults={"title": title, "invite_link": invite_link, "is_active": True},
    )
    return obj.id


@sync_to_async
def record_action(
    *,
    chat_id: int,
    userbot_id: int,
    spammer_id: int,
    spammer_username: str,
    action: str,
    stage: str,
    detail: str,
    message_id: int | None = None,
) -> None:
    from agents.models import SecurityLog, TelegramGroup

    group = TelegramGroup.objects.filter(
        chat_id=chat_id, monitored_by_id=userbot_id
    ).first()
    SecurityLog.objects.create(
        group=group,
        handled_by_id=userbot_id,
        spammer_id=spammer_id,
        spammer_username=spammer_username or "",
        message_id=message_id,
        action_taken=action,
        stage=stage,
        detail=detail[:500],
    )


@sync_to_async
def add_to_blacklist(
    *,
    telegram_id: int,
    username: str = "",
    first_name: str = "",
    bio: str = "",
    reason: str = "",
) -> None:
    from agents.models import BlacklistUser

    BlacklistUser.objects.update_or_create(
        telegram_id=telegram_id,
        defaults={
            "username": username or "",
            "first_name": first_name or "",
            "bio": bio or "",
            "reason_text": reason[:255],
            "detected_at": timezone.now(),
        },
    )


@sync_to_async
def cache_spam_content(*, content_type: str, content_hash: str, raw_data: str = "") -> None:
    from agents.models import SpamContent

    SpamContent.objects.get_or_create(
        content_type=content_type,
        content_hash=content_hash,
        defaults={"raw_data": raw_data[:2000]},
    )



@sync_to_async
def claim_pending_jobs() -> list[dict]:
    """
    Atomically move PENDING propagation jobs to RUNNING and return their data.

    Returning a plain list of dicts keeps the async caller away from lazy ORM
    attribute access.
    """
    from agents.models import PropagationJob

    jobs: list[dict] = []
    pending_ids = list(
        PropagationJob.objects.filter(
            status=PropagationJob.Status.PENDING
        ).values_list("id", flat=True)
    )
    for job_id in pending_ids:
        # Claim one-by-one so two engine instances never grab the same job.
        claimed = PropagationJob.objects.filter(
            id=job_id, status=PropagationJob.Status.PENDING
        ).update(status=PropagationJob.Status.RUNNING)
        if not claimed:
            continue
        job = PropagationJob.objects.select_related("admin_bot", "new_bot").get(id=job_id)
        jobs.append(
            {
                "id": job.id,
                "admin_bot_id": job.admin_bot_id,
                "new_bot_id": job.new_bot_id,
                "new_bot_telegram_id": job.new_bot.telegram_id,
                "chat_id": job.chat_id,
                "admin_title": job.admin_title or "Anti-Spam Agent",
            }
        )
    return jobs


@sync_to_async
def finish_job(job_id: int, status: str, result: str) -> None:
    from agents.models import PropagationJob

    PropagationJob.objects.filter(id=job_id).update(status=status, result=result[:500])



# --------------------------------------------------------------------------- #
#  Engine command queue (dashboard -> engine RPC)
# --------------------------------------------------------------------------- #
@sync_to_async
def claim_pending_commands() -> list[dict]:
    """Atomically claim PENDING EngineCommands (move to RUNNING) and return them."""
    from agents.models import EngineCommand

    cmds: list[dict] = []
    pending_ids = list(
        EngineCommand.objects.filter(
            status=EngineCommand.Status.PENDING
        ).values_list("id", flat=True)
    )
    for cmd_id in pending_ids:
        claimed = EngineCommand.objects.filter(
            id=cmd_id, status=EngineCommand.Status.PENDING
        ).update(status=EngineCommand.Status.RUNNING)
        if not claimed:
            continue
        cmd = EngineCommand.objects.get(id=cmd_id)
        cmds.append(
            {
                "id": cmd.id,
                "userbot_id": cmd.userbot_id,
                "kind": cmd.kind,
                "chat_id": cmd.chat_id,
                "limit": cmd.limit,
            }
        )
    return cmds


@sync_to_async
def finish_command(cmd_id: int, status: str, result: str) -> None:
    from agents.models import EngineCommand

    EngineCommand.objects.filter(id=cmd_id).update(status=status, result=result[:500])


# --------------------------------------------------------------------------- #
#  Dialog (chat list) cache
# --------------------------------------------------------------------------- #
@sync_to_async
def upsert_dialog(userbot_id: int, data: dict) -> None:
    from agents.models import TelegramDialog, TelegramGroup

    # Keep the "monitored" mirror in sync with the actual TelegramGroup state.
    monitored = TelegramGroup.objects.filter(
        monitored_by_id=userbot_id, chat_id=data["chat_id"], is_active=True
    ).exists()
    TelegramDialog.objects.update_or_create(
        userbot_id=userbot_id,
        chat_id=data["chat_id"],
        defaults={
            "dialog_type": data["dialog_type"],
            "title": data.get("title", "")[:255],
            "username": data.get("username", "")[:64],
            "is_admin": data.get("is_admin", False),
            "members_count": data.get("members_count"),
            "last_message": data.get("last_message", "")[:512],
            "last_message_date": data.get("last_message_date"),
            "monitored": monitored,
        },
    )


@sync_to_async
def prune_dialogs(userbot_id: int, keep_chat_ids: list[int]) -> None:
    """Drop cached dialogs that no longer appear in the latest sync."""
    from agents.models import TelegramDialog

    TelegramDialog.objects.filter(userbot_id=userbot_id).exclude(
        chat_id__in=keep_chat_ids
    ).delete()


# --------------------------------------------------------------------------- #
#  Message cache
# --------------------------------------------------------------------------- #
@sync_to_async
def replace_chat_messages(userbot_id: int, chat_id: int, messages: list[dict]) -> int:
    from agents.models import ChatMessage

    ChatMessage.objects.filter(userbot_id=userbot_id, chat_id=chat_id).delete()
    objs = [
        ChatMessage(
            userbot_id=userbot_id,
            chat_id=chat_id,
            message_id=m["message_id"],
            sender_id=m.get("sender_id"),
            sender_name=m.get("sender_name", "")[:128],
            sender_username=m.get("sender_username", "")[:64],
            text=m.get("text", ""),
            media_type=m.get("media_type", "")[:24],
            outgoing=m.get("outgoing", False),
            date=m.get("date"),
        )
        for m in messages
    ]
    ChatMessage.objects.bulk_create(objs, ignore_conflicts=True)
    return len(objs)



# --------------------------------------------------------------------------- #
#  Monitored-group archive + config
# --------------------------------------------------------------------------- #
@sync_to_async
def group_config(userbot_id: int, chat_id: int) -> dict | None:
    """Return monitoring config for a (bot, chat): id, admin flag, helper bot."""
    from agents.models import TelegramGroup

    g = TelegramGroup.objects.filter(
        monitored_by_id=userbot_id, chat_id=chat_id, is_active=True
    ).first()
    if not g:
        return None
    return {
        "group_id": g.id,
        "monitor_is_admin": g.monitor_is_admin,
        "helper_bot_id": g.helper_bot_id,
    }


@sync_to_async
def helper_telegram_id(helper_bot_id: int) -> int | None:
    from agents.models import UserBot

    bot = UserBot.objects.filter(id=helper_bot_id).first()
    return bot.telegram_id if bot else None


@sync_to_async
def save_group_message(userbot_id: int, chat_id: int, data: dict) -> None:
    """Archive one monitored-group message (append-only, enriched with role)."""
    from agents.models import GroupMessage, TelegramGroup

    group = TelegramGroup.objects.filter(
        monitored_by_id=userbot_id, chat_id=chat_id
    ).first()
    if not group:
        return
    GroupMessage.objects.update_or_create(
        userbot_id=userbot_id,
        chat_id=chat_id,
        message_id=data["message_id"],
        defaults={
            "group_id": group.id,
            "sender_id": data.get("sender_id"),
            "sender_name": data.get("sender_name", "")[:128],
            "sender_username": data.get("sender_username", "")[:64],
            "sender_role": data.get("sender_role", "user"),
            "tg_scam_flag": data.get("tg_scam_flag", False),
            "text": data.get("text", ""),
            "media_type": data.get("media_type", "")[:24],
            "date": data.get("date"),
        },
    )


@sync_to_async
def set_group_message_role(
    userbot_id: int, chat_id: int, message_id: int, role: str
) -> None:
    from agents.models import GroupMessage

    GroupMessage.objects.filter(
        userbot_id=userbot_id, chat_id=chat_id, message_id=message_id
    ).update(sender_role=role)


@sync_to_async
def set_monitor_admin_flag(userbot_id: int, chat_id: int, is_admin: bool) -> None:
    from agents.models import TelegramGroup

    TelegramGroup.objects.filter(
        monitored_by_id=userbot_id, chat_id=chat_id
    ).update(monitor_is_admin=is_admin)


@sync_to_async
def is_blacklisted(telegram_id: int) -> bool:
    from agents.models import BlacklistUser

    return BlacklistUser.objects.filter(telegram_id=telegram_id).exists()



@sync_to_async
def count_actions_in_hours(userbot_id: int, hours: int) -> dict:
    """Count enforcement actions in the last N hours for a specific userbot."""
    import datetime as _dt
    from agents.models import SecurityLog

    since = timezone.now() - _dt.timedelta(hours=hours)
    qs = SecurityLog.objects.filter(handled_by_id=userbot_id, timestamp__gte=since)
    total = qs.count()
    deleted_banned = qs.filter(action_taken="deleted_banned").count()
    reported = qs.filter(action_taken="reported").count()
    flagged = qs.filter(action_taken="flagged").count()
    return {
        "total": total,
        "deleted_banned": deleted_banned,
        "reported": reported,
        "flagged": flagged,
    }
