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
    from agents.models import SecurityLog

    since = timezone.now() - __import__("datetime").timedelta(hours=hours)
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


# --------------------------------------------------------------------------- #
#  3-Strike System
# --------------------------------------------------------------------------- #
@sync_to_async
def get_or_create_strike(group_id: int, chat_id: int, user_id: int,
                         username: str = "", first_name: str = "") -> dict:
    """Get or create a strike record for a user in a group. Returns dict."""
    from agents.models import SpamStrike, TelegramGroup

    group = TelegramGroup.objects.filter(id=group_id).first()
    if not group:
        group = TelegramGroup.objects.filter(chat_id=chat_id).first()
    if not group:
        return {"strike_count": 0, "is_banned": False}

    obj, _ = SpamStrike.objects.get_or_create(
        group=group,
        user_id=user_id,
        defaults={
            "username": username[:64],
            "first_name": first_name[:128],
        },
    )
    return {
        "id": obj.id,
        "strike_count": obj.strike_count,
        "is_banned": obj.is_banned,
        "last_strike_at": obj.last_strike_at,
    }


@sync_to_async
def increment_strike(group_id: int, chat_id: int, user_id: int,
                     username: str = "", first_name: str = "") -> dict:
    """
    Add a strike. Returns updated strike info.
    If strike_count reaches 3, marks is_banned=True.
    Strikes reset if >24h since last strike.
    """
    import datetime as _dt
    from agents.models import SpamStrike, TelegramGroup

    group = TelegramGroup.objects.filter(id=group_id).first()
    if not group:
        group = TelegramGroup.objects.filter(chat_id=chat_id).first()
    if not group:
        return {"strike_count": 1, "is_banned": False, "should_ban": False}

    obj, created = SpamStrike.objects.get_or_create(
        group=group,
        user_id=user_id,
        defaults={
            "username": username[:64],
            "first_name": first_name[:128],
            "strike_count": 0,
        },
    )

    # Reset strikes if more than 24h since last one (user might have recovered)
    if not created and obj.last_strike_at:
        elapsed = timezone.now() - obj.last_strike_at
        if elapsed > _dt.timedelta(hours=24):
            obj.strike_count = 0

    obj.strike_count += 1
    obj.last_strike_at = timezone.now()
    obj.username = username[:64] or obj.username
    obj.first_name = first_name[:128] or obj.first_name

    should_ban = obj.strike_count >= 3
    if should_ban:
        obj.is_banned = True

    obj.save()
    return {
        "id": obj.id,
        "strike_count": obj.strike_count,
        "is_banned": obj.is_banned,
        "should_ban": should_ban,
    }


@sync_to_async
def is_strike_banned(chat_id: int, user_id: int) -> bool:
    """Check if user is already strike-banned in this group."""
    from agents.models import SpamStrike, TelegramGroup

    group = TelegramGroup.objects.filter(chat_id=chat_id).first()
    if not group:
        return False
    return SpamStrike.objects.filter(
        group=group, user_id=user_id, is_banned=True
    ).exists()


# --------------------------------------------------------------------------- #
#  Join Event Logging
# --------------------------------------------------------------------------- #
@sync_to_async
def record_join_event(
    *,
    chat_id: int,
    userbot_id: int,
    user_id: int,
    username: str = "",
    first_name: str = "",
    bio: str = "",
    scan_result: str = "clean",
    action_taken: str = "allowed",
    detail: str = "",
) -> None:
    """Record a new member join event with scan results."""
    from agents.models import JoinEvent, TelegramGroup

    group = TelegramGroup.objects.filter(
        chat_id=chat_id, monitored_by_id=userbot_id
    ).first()
    if not group:
        return

    JoinEvent.objects.create(
        group=group,
        userbot_id=userbot_id,
        user_id=user_id,
        username=username[:64],
        first_name=first_name[:128],
        bio=bio[:2000],
        scan_result=scan_result,
        action_taken=action_taken,
        detail=detail[:2000],
    )


@sync_to_async
def count_join_events_today() -> dict:
    """Count join events for today grouped by scan result."""
    from agents.models import JoinEvent

    today = timezone.now().date()
    qs = JoinEvent.objects.filter(timestamp__date=today)
    total = qs.count()
    banned = qs.filter(action_taken="banned").count()
    clean = qs.filter(scan_result="clean").count()
    nsfw = qs.exclude(scan_result="clean").exclude(scan_result="scan_failed").count()
    return {
        "total": total,
        "banned": banned,
        "clean": clean,
        "nsfw": nsfw,
    }


@sync_to_async
def count_strikes_today() -> dict:
    """Count strike events for today."""
    from agents.models import SecurityLog

    today = timezone.now().date()
    warnings = SecurityLog.objects.filter(
        action_taken="strike_warn", timestamp__date=today
    ).count()
    bans = SecurityLog.objects.filter(
        action_taken="strike_ban", timestamp__date=today
    ).count()
    return {"warnings": warnings, "bans": bans}
