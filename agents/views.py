"""
Dashboard views.

The HTML pages are thin; most live data is delivered through small JSON
endpoints that the templates poll with AJAX (every 2s) - this is what powers the
real-time QR login screen and the live security-log table without WebSockets.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

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


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
@login_required
def dashboard(request):
    today = timezone.now().date()
    ctx = {
        "total_groups": TelegramGroup.objects.filter(is_active=True).count(),
        "active_agents": UserBot.objects.filter(status=UserBot.Status.ACTIVE).count(),
        "total_agents": UserBot.objects.count(),
        "blocked_today": SecurityLog.objects.filter(
            action_taken=SecurityLog.Action.DELETED_AND_BANNED,
            timestamp__date=today,
        ).count(),
        "blacklist_size": BlacklistUser.objects.count(),
        "spam_signatures": SpamContent.objects.count(),
        "active": "dashboard",
    }
    return render(request, "agents/dashboard.html", ctx)


@login_required
def logs_page(request):
    return render(request, "agents/logs.html", {"active": "logs"})


@login_required
def userbots_page(request):
    bots = UserBot.objects.all().prefetch_related("groups")
    return render(request, "agents/userbots.html", {"bots": bots, "active": "userbots"})


@login_required
def qr_add_page(request):
    return render(request, "agents/qr_add.html", {"active": "qr_add"})


# --------------------------------------------------------------------------- #
#  Filter rules (stage-2 detection) management
# --------------------------------------------------------------------------- #
@login_required
def filters_page(request):
    rules = FilterRule.objects.all()
    ctx = {
        "active": "filters",
        "keywords": rules.filter(rule_type=FilterRule.RuleType.KEYWORD),
        "emojis": rules.filter(rule_type=FilterRule.RuleType.EMOJI),
        "regexes": rules.filter(rule_type=FilterRule.RuleType.REGEX),
        "rule_types": FilterRule.RuleType.choices,
    }
    return render(request, "agents/filters.html", ctx)


@login_required
@require_POST
def add_rule(request):
    rule_type = request.POST.get("rule_type", "")
    pattern = (request.POST.get("pattern") or "").strip()
    note = (request.POST.get("note") or "").strip()
    valid_types = {c[0] for c in FilterRule.RuleType.choices}

    if rule_type not in valid_types or not pattern:
        messages.error(request, "A rule type and a non-empty pattern are required.")
        return redirect("agents:filters")

    # Validate regex patterns before saving so we never feed a broken pattern
    # to the engine.
    if rule_type == FilterRule.RuleType.REGEX:
        try:
            re.compile(pattern)
        except re.error as exc:
            messages.error(request, f"Invalid regex: {exc}")
            return redirect("agents:filters")

    _, created = FilterRule.objects.get_or_create(
        rule_type=rule_type, pattern=pattern, defaults={"note": note}
    )
    messages.success(request, "Rule added." if created else "Rule already exists.")
    _reset_engine_rule_cache()
    return redirect("agents:filters")


@login_required
@require_POST
def toggle_rule(request, rule_id: int):
    rule = get_object_or_404(FilterRule, pk=rule_id)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active"])
    _reset_engine_rule_cache()
    return redirect("agents:filters")


@login_required
@require_POST
def delete_rule(request, rule_id: int):
    get_object_or_404(FilterRule, pk=rule_id).delete()
    _reset_engine_rule_cache()
    return redirect("agents:filters")


def _reset_engine_rule_cache():
    """Best-effort cache bust for the current process (engine uses a TTL too)."""
    try:
        from .engine.filters import reset_rule_cache

        reset_rule_cache()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  Group propagation (invite + auto-promote) via job queue
# --------------------------------------------------------------------------- #
@login_required
def propagation_page(request):
    ctx = {
        "active": "propagation",
        "active_bots": UserBot.objects.filter(status=UserBot.Status.ACTIVE),
        "all_bots": UserBot.objects.all(),
        "jobs": PropagationJob.objects.select_related("admin_bot", "new_bot")[:25],
    }
    return render(request, "agents/propagation.html", ctx)


@login_required
@require_POST
def create_propagation(request):
    try:
        admin_bot = UserBot.objects.get(pk=request.POST.get("admin_bot"))
        new_bot = UserBot.objects.get(pk=request.POST.get("new_bot"))
        chat_id = int(request.POST.get("chat_id"))
    except (UserBot.DoesNotExist, TypeError, ValueError):
        messages.error(request, "Please choose both userbots and a valid chat id.")
        return redirect("agents:propagation")

    if admin_bot.pk == new_bot.pk:
        messages.error(request, "Admin and target userbots must be different.")
        return redirect("agents:propagation")

    PropagationJob.objects.create(
        admin_bot=admin_bot,
        new_bot=new_bot,
        chat_id=chat_id,
        admin_title=(request.POST.get("admin_title") or "Anti-Spam Agent")[:64],
    )
    messages.success(
        request,
        "Propagation job queued. The engine (run_agents) will execute it shortly.",
    )
    return redirect("agents:propagation")


# --------------------------------------------------------------------------- #
#  JSON: charts / stats
# --------------------------------------------------------------------------- #
@login_required
def api_stats(request):
    """Daily 'deleted+banned' counts for the last 7 days + stage breakdown."""
    since = timezone.now() - timedelta(days=6)
    daily = (
        SecurityLog.objects.filter(
            timestamp__gte=since,
            action_taken=SecurityLog.Action.DELETED_AND_BANNED,
        )
        .annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    by_day = {row["day"].isoformat(): row["count"] for row in daily}

    labels, values = [], []
    for i in range(6, -1, -1):
        d = (timezone.now() - timedelta(days=i)).date().isoformat()
        labels.append(d)
        values.append(by_day.get(d, 0))

    stage_rows = (
        SecurityLog.objects.filter(action_taken=SecurityLog.Action.DELETED_AND_BANNED)
        .values("stage")
        .annotate(count=Count("id"))
    )
    stages = {row["stage"] or "unknown": row["count"] for row in stage_rows}

    return JsonResponse(
        {
            "daily": {"labels": labels, "values": values},
            "stages": {"labels": list(stages.keys()), "values": list(stages.values())},
        }
    )


@login_required
def api_logs(request):
    """Most recent security log rows for the live table."""
    rows = (
        SecurityLog.objects.select_related("group", "handled_by")
        .all()[:50]
    )
    data = [
        {
            "timestamp": timezone.localtime(r.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            "group": r.group.title if r.group else "-",
            "agent": (r.handled_by.username or f"bot#{r.handled_by_id}")
            if r.handled_by
            else "-",
            "spammer": r.spammer_username or str(r.spammer_id or "-"),
            "action": r.get_action_taken_display(),
            "stage": r.stage,
            "detail": r.detail,
        }
        for r in rows
    ]
    return JsonResponse({"logs": data})


# --------------------------------------------------------------------------- #
#  JSON: QR login flow
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def api_qr_start(request):
    """Kick off a new QR-login session and return the first QR image."""
    from .engine.qr_login import manager as qr_manager

    state = qr_manager().start()
    # Give the background coroutine a brief moment to publish the first token.
    return JsonResponse({"session_id": state.session_id, "status": state.status})


@login_required
def api_qr_status(request):
    """Poll the live state of a QR-login session (called every ~2s)."""
    from .engine.qr_login import manager as qr_manager

    sid = request.GET.get("sid", "")
    state = qr_manager().get(sid)
    if not state:
        return JsonResponse({"status": "error", "error": "unknown session"}, status=404)
    return JsonResponse(
        {
            "status": state.status,
            "qr_png_b64": state.qr_png_b64,
            "login_url": state.login_url,
            "expires_at": state.expires_at,
            "error": state.error,
            "password_error": state.password_error,
            "userbot_id": state.userbot_id,
            "username": state.username,
        }
    )


@login_required
@require_POST
def api_qr_password(request):
    """Submit the 2FA cloud password for an in-flight QR-login session."""
    from .engine.qr_login import manager as qr_manager

    sid = request.POST.get("sid", "")
    password = request.POST.get("password", "")
    ok = qr_manager().submit_password(sid, password)
    if not ok:
        return JsonResponse(
            {"ok": False, "error": "session not waiting for a password"}, status=400
        )
    return JsonResponse({"ok": True})


@login_required
def api_jobs(request):
    """Live propagation-job statuses for the propagation page table."""
    rows = PropagationJob.objects.select_related("admin_bot", "new_bot")[:25]
    data = [
        {
            "id": j.id,
            "admin": j.admin_bot.username or f"bot#{j.admin_bot_id}",
            "new": j.new_bot.username or f"bot#{j.new_bot_id}",
            "chat_id": j.chat_id,
            "status": j.status,
            "result": j.result,
            "created": timezone.localtime(j.created_at).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for j in rows
    ]
    return JsonResponse({"jobs": data})


# --------------------------------------------------------------------------- #
#  Group management actions
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def toggle_group(request, group_id: int):
    group = get_object_or_404(TelegramGroup, pk=group_id)
    group.is_active = not group.is_active
    group.save(update_fields=["is_active"])
    return redirect("agents:userbots")


@login_required
@require_POST
def delete_userbot(request, bot_id: int):
    get_object_or_404(UserBot, pk=bot_id).delete()
    return redirect("agents:userbots")



# --------------------------------------------------------------------------- #
#  Telegram-like chat browser
# --------------------------------------------------------------------------- #
_DIALOG_FILTERS = {
    "all": "All chats",
    "private": "Users",
    "bot": "Bots",
    "group": "Groups",
    "channel": "Channels",
    "admin": "Admin groups",
}


def _selected_bot(request):
    """Resolve the userbot whose chats we are browsing (?bot=<id>)."""
    bots = UserBot.objects.all()
    bot_id = request.GET.get("bot")
    bot = None
    if bot_id:
        bot = bots.filter(pk=bot_id).first()
    if bot is None:
        bot = bots.filter(status=UserBot.Status.ACTIVE).first() or bots.first()
    return bot, bots


@login_required
def chats_page(request):
    bot, bots = _selected_bot(request)
    flt = request.GET.get("filter", "all")
    if flt not in _DIALOG_FILTERS:
        flt = "all"

    dialogs = TelegramDialog.objects.none()
    counts = {}
    if bot:
        base = TelegramDialog.objects.filter(userbot=bot)
        counts = {
            "all": base.count(),
            "private": base.filter(dialog_type="private").count(),
            "bot": base.filter(dialog_type="bot").count(),
            "group": base.filter(dialog_type__in=["group", "supergroup"]).count(),
            "channel": base.filter(dialog_type="channel").count(),
            "admin": base.filter(is_admin=True).count(),
        }
        if flt == "group":
            dialogs = base.filter(dialog_type__in=["group", "supergroup"])
        elif flt == "admin":
            dialogs = base.filter(is_admin=True)
        elif flt == "all":
            dialogs = base
        else:
            dialogs = base.filter(dialog_type=flt)

    ctx = {
        "active": "chats",
        "bot": bot,
        "bots": bots,
        "dialogs": dialogs,
        "filter": flt,
        "filters": _DIALOG_FILTERS,
        "counts": counts,
        # Other active accounts that can act as a helper (admin) for groups
        # where the monitoring bot lacks admin rights.
        "helper_bots": bots.filter(status=UserBot.Status.ACTIVE).exclude(pk=bot.pk)
        if bot
        else UserBot.objects.none(),
    }
    return render(request, "agents/chats.html", ctx)


@login_required
def chat_messages_page(request, bot_id: int, chat_id: int):
    bot = get_object_or_404(UserBot, pk=bot_id)
    dialog = TelegramDialog.objects.filter(userbot=bot, chat_id=chat_id).first()
    ctx = {
        "active": "chats",
        "bot": bot,
        "dialog": dialog,
        "chat_id": chat_id,
    }
    return render(request, "agents/chat_messages.html", ctx)


@login_required
@require_POST
def sync_chats(request, bot_id: int):
    bot = get_object_or_404(UserBot, pk=bot_id)
    EngineCommand.objects.create(userbot=bot, kind=EngineCommand.Kind.SYNC_DIALOGS)
    messages.success(
        request,
        "Chat-list sync queued. The engine (run_agents) will refresh it shortly.",
    )
    return redirect(f"{reverse('agents:chats')}?bot={bot.id}")


@login_required
@require_POST
def fetch_history(request, bot_id: int, chat_id: int):
    bot = get_object_or_404(UserBot, pk=bot_id)
    limit = int(request.POST.get("limit", 50) or 50)
    EngineCommand.objects.create(
        userbot=bot,
        kind=EngineCommand.Kind.FETCH_HISTORY,
        chat_id=chat_id,
        limit=max(1, min(limit, 200)),
    )
    messages.success(request, "Message fetch queued. Refreshing shortly...")
    return redirect("agents:chat_messages", bot_id=bot.id, chat_id=chat_id)


@login_required
@require_POST
def toggle_antispam(request, dialog_id: int):
    """Enable/disable anti-spam monitoring for a group (takes effect live)."""
    dialog = get_object_or_404(TelegramDialog, pk=dialog_id)
    flt = request.GET.get("filter", "group")
    back = f"{reverse('agents:chats')}?bot={dialog.userbot_id}&filter={flt}"
    if not dialog.is_group:
        messages.error(request, "Anti-spam can only be enabled on groups.")
        return redirect(back)

    if dialog.monitored:
        TelegramGroup.objects.filter(
            monitored_by=dialog.userbot, chat_id=dialog.chat_id
        ).update(is_active=False)
        dialog.monitored = False
        dialog.save(update_fields=["monitored"])
        messages.info(request, f"Anti-spam disabled for {dialog.title or dialog.chat_id}.")
        return redirect(back)

    # Enabling. If the monitoring bot is not admin, a helper account is needed.
    helper_bot = None
    helper_id = request.POST.get("helper_bot")
    if helper_id:
        helper_bot = UserBot.objects.filter(pk=helper_id).first()

    if not dialog.is_admin and not helper_bot:
        messages.error(
            request,
            f"'{dialog.title or dialog.chat_id}': this account is not admin here. "
            "Choose a helper (admin) account to enable anti-spam.",
        )
        return redirect(back)

    TelegramGroup.objects.update_or_create(
        monitored_by=dialog.userbot,
        chat_id=dialog.chat_id,
        defaults={
            "title": dialog.title,
            "is_active": True,
            "monitor_is_admin": dialog.is_admin,
            "helper_bot": helper_bot,
        },
    )
    dialog.monitored = True
    dialog.save(update_fields=["monitored"])
    if dialog.is_admin:
        messages.success(request, f"Anti-spam enabled for {dialog.title or dialog.chat_id}.")
    else:
        messages.success(
            request,
            f"Anti-spam enabled for {dialog.title or dialog.chat_id} via helper "
            f"{helper_bot.username or helper_bot.id}.",
        )
    return redirect(back)


@login_required
def group_archive_page(request, bot_id: int, chat_id: int):
    bot = get_object_or_404(UserBot, pk=bot_id)
    group = TelegramGroup.objects.filter(monitored_by=bot, chat_id=chat_id).first()
    return render(
        request,
        "agents/group_archive.html",
        {"active": "chats", "bot": bot, "group": group, "chat_id": chat_id},
    )


@login_required
def api_group_messages(request, bot_id: int, chat_id: int):
    role = request.GET.get("role", "")
    qs = GroupMessage.objects.filter(userbot_id=bot_id, chat_id=chat_id)
    if role in {"admin", "user", "scam", "bot", "me"}:
        qs = qs.filter(sender_role=role)
    rows = qs[:200]
    data = [
        {
            "message_id": m.message_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "sender_username": m.sender_username,
            "role": m.sender_role,
            "tg_scam": m.tg_scam_flag,
            "text": m.text,
            "media_type": m.media_type,
            "date": timezone.localtime(m.date).strftime("%Y-%m-%d %H:%M") if m.date else "",
        }
        for m in rows
    ]
    counts = {
        "all": GroupMessage.objects.filter(userbot_id=bot_id, chat_id=chat_id).count(),
        "scam": GroupMessage.objects.filter(userbot_id=bot_id, chat_id=chat_id, sender_role="scam").count(),
        "admin": GroupMessage.objects.filter(userbot_id=bot_id, chat_id=chat_id, sender_role="admin").count(),
    }
    return JsonResponse({"messages": data, "counts": counts})


# --------------------------------------------------------------------------- #
#  JSON: chat browser live data
# --------------------------------------------------------------------------- #
@login_required
def api_command_status(request):
    """Latest command status for a bot (so the UI can show 'syncing...')."""
    bot_id = request.GET.get("bot")
    cmd = (
        EngineCommand.objects.filter(userbot_id=bot_id).order_by("-created_at").first()
        if bot_id
        else None
    )
    if not cmd:
        return JsonResponse({"status": "none"})
    return JsonResponse(
        {"kind": cmd.kind, "status": cmd.status, "result": cmd.result}
    )


@login_required
def api_messages(request, bot_id: int, chat_id: int):
    rows = ChatMessage.objects.filter(userbot_id=bot_id, chat_id=chat_id)
    data = [
        {
            "message_id": m.message_id,
            "sender_name": m.sender_name or (str(m.sender_id) if m.sender_id else "—"),
            "sender_username": m.sender_username,
            "text": m.text,
            "media_type": m.media_type,
            "outgoing": m.outgoing,
            "date": timezone.localtime(m.date).strftime("%Y-%m-%d %H:%M")
            if m.date
            else "",
        }
        for m in rows
    ]
    return JsonResponse({"messages": data})
