"""
The agent runner: boots every active userbot and guards its monitored groups.

For each active `UserBot` we build a Pyrogram client from its stored
StringSession and attach a single message handler scoped to the chats that bot
monitors. Each message flows through the hybrid filter pipeline; confirmed spam
is deleted, the sender banned, and (for AI-confirmed novel spam) the payload is
fingerprinted into `SpamContent` and the user added to `BlacklistUser` so the
next occurrence is blocked instantly without an AI call.

All clients share the one background event loop and run concurrently.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from django.conf import settings
from django.utils import timezone as djtz

from pyrogram import Client, filters as pf
from pyrogram.enums import ChatType, ChatMemberStatus
from pyrogram.enums import ChatType, ChatMemberStatus, ChatMembersFilter
from pyrogram.errors import ChatAdminRequired
from pyrogram.handlers import MessageHandler

from . import repository as repo
from .filters import ai_decide, precheck
from .hashing import image_phash, text_hash
from .propagation import delete_and_ban, propagate_agent, safe_call, throttle

logger = logging.getLogger(__name__)

# How long the engine caches each bot's monitored-chat set (seconds). A short
# TTL lets dashboard anti-spam toggles take effect without restarting the engine.
_MONITOR_TTL = 12.0


class AgentRunner:
    def __init__(self) -> None:
        self.clients: dict[int, Client] = {}
        self._running = False
        self._job_task: asyncio.Task | None = None
        self._cmd_task: asyncio.Task | None = None
        # bot_id -> (set_of_chat_ids, loaded_at)
        self._monitor_cache: dict[int, tuple[set[int], float]] = {}

    async def start(self) -> None:
        self._running = True
        bots = await repo.list_active_userbots()
        if not bots:
            logger.warning("No active userbots found. Add one via the dashboard QR flow.")
        for bot in bots:
            await self._start_one(bot)
        # Background tasks: propagation jobs + dashboard read commands.
        self._job_task = asyncio.ensure_future(self._job_loop())
        self._cmd_task = asyncio.ensure_future(self._command_loop())
        logger.info("Agent runner online with %d userbot(s).", len(self.clients))

    async def _is_monitored(self, bot_id: int, chat_id: int) -> bool:
        """TTL-cached lookup of whether a chat is anti-spam monitored right now."""
        cached = self._monitor_cache.get(bot_id)
        now = time.time()
        if cached is None or now - cached[1] > _MONITOR_TTL:
            chat_ids = set(await repo.monitored_chat_ids(bot_id))
            self._monitor_cache[bot_id] = (chat_ids, now)
            cached = self._monitor_cache[bot_id]
        return chat_id in cached[0]

    async def _start_one(self, bot: dict) -> None:
        client = Client(
            name=f"agent-{bot['id']}",
            api_id=bot["api_id"] or settings.TELEGRAM_API_ID,
            api_hash=bot["api_hash"] or settings.TELEGRAM_API_HASH,
            session_string=bot["session_string"],
            workdir=str(settings.PYROGRAM_WORKDIR),
        )

        async def handler(client: Client, message, _bot_id=bot["id"]):
            try:
                if not message.chat:
                    return
                # Dynamic check so toggling anti-spam in the dashboard is live.
                if not await self._is_monitored(_bot_id, message.chat.id):
                    return
                await self._on_message(client, message, _bot_id)
            except Exception as exc:  # noqa: BLE001 - never kill the listener
                logger.exception("handler error: %s", exc)

        # Only react to group/supergroup messages from real users.
        client.add_handler(
            MessageHandler(handler, pf.group & ~pf.service & ~pf.me)
        )

        # /count N command in private chat or Saved Messages.
        async def count_handler(client: Client, message, _bot_id=bot["id"]):
            try:
                await self._handle_count_command(client, message, _bot_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("/count handler error: %s", exc)

        client.add_handler(
            MessageHandler(
                count_handler,
                pf.private & pf.command("count"),
            )
        )
        await client.start()
        self.clients[bot["id"]] = client
        await repo.set_bot_status(bot["id"], "active")
        logger.info("UserBot #%s (%s) started.", bot["id"], bot.get("username"))

    async def _on_message(self, client: Client, message, bot_id: int) -> None:
        sender = message.from_user
        if not sender:
            return

        text = message.text or message.caption or ""
        content_type, content_hash, photo_path = await self._fingerprint(client, message)

        try:
            # --- Stages 1 & 2 (cheap, no network) ------------------------- #
            verdict = await precheck(
                text=text,
                sender_id=sender.id,
                content_type=content_type,
                content_hash=content_hash,
            )

            # --- Stage 3 escalation: enrich with bio + profile photo ------ #
            if verdict.needs_ai:
                bio, sender_photo = await self._enrich_sender(client, sender.id)
                verdict = await ai_decide(
                    text=text,
                    bio=bio,
                    photo_path=sender_photo,
                    stage2_reason=verdict.reason,
                )
                self._cleanup(sender_photo)

                if not verdict.is_spam:
                    # AI cleared it - keep the message but log for review.
                    await repo.record_action(
                        chat_id=message.chat.id,
                        userbot_id=bot_id,
                        spammer_id=sender.id,
                        spammer_username=sender.username or "",
                        action="flagged",
                        stage=verdict.stage,
                        detail=verdict.reason,
                    )
                    return

            if not verdict.is_spam:
                return  # clean message

            # --- Confirmed spam: enforce ---------------------------------- #
            await delete_and_ban(client, message.chat.id, message.id, sender.id)
            await repo.record_action(
                chat_id=message.chat.id,
                userbot_id=bot_id,
                spammer_id=sender.id,
                spammer_username=sender.username or "",
                action="deleted_banned",
                stage=verdict.stage,
                detail=verdict.reason,
            )

            # Persist learning so future identical spam skips the AI entirely.
            if verdict.should_cache:
                await repo.add_to_blacklist(
                    telegram_id=sender.id,
                    username=sender.username or "",
                    first_name=sender.first_name or "",
                    reason=verdict.reason,
                )
                if content_hash:
                    await repo.cache_spam_content(
                        content_type=content_type,
                        content_hash=content_hash,
                        raw_data=text,
                    )

            logger.info(
                "BANNED %s in chat %s [%s] - %s",
                sender.id, message.chat.id, verdict.stage, verdict.reason,
            )
            # Enforce - directly if admin, else fallback to helper/saved.
            # _enforce also blacklists the user + caches the content hash.
            await self._enforce(client, message, bot_id, sender, verdict)
        finally:
            self._cleanup(photo_path)

    async def _enforce(self, client, message, bot_id, sender, verdict) -> None:
        chat_id = message.chat.id
        text = message.text or message.caption or ""
        cfg = await repo.group_config(bot_id, chat_id)

        # Always blacklist + cache the spam content (regardless of admin rights).
        if verdict.should_cache:
            await repo.add_to_blacklist(
                telegram_id=sender.id,
                username=sender.username or "",
                first_name=sender.first_name or "",
                reason=verdict.reason,
            )
        # Cache the content hash so this payload is blocked instantly next time.
        from .hashing import text_hash as _th
        content_hash = _th(text) if text.strip() else None
        if content_hash:
            await repo.cache_spam_content(
                content_type="text", content_hash=content_hash, raw_data=text,
            )

        # Try to enforce directly (delete + ban).
        try:
            await delete_and_ban(client, chat_id, message.id, sender.id)
            await repo.record_action(
                chat_id=chat_id, userbot_id=bot_id, spammer_id=sender.id,
                spammer_username=sender.username or "", message_id=message.id,
                action="deleted_banned", stage=verdict.stage, detail=verdict.reason,
            )
            logger.info("BANNED %s in chat %s [%s]", sender.id, chat_id, verdict.stage)
            return
        except ChatAdminRequired:
            # Not admin — graceful fallback below.
            logger.warning(
                "Not admin in chat %s, falling back to helper/saved", chat_id,
            )

        # --- Fallback: report to helper account or Saved Messages ---------- #
        helper_id = cfg.get("helper_bot_id") if cfg else None
        reported = await self._report_to_helper(
            helper_id, chat_id, message.id, sender.id, verdict.reason
        )

        if not reported:
            # No helper available — write to the monitoring bot's own Saved Messages.
            note = (
                "🚨 Anti-Spam: no admin rights\n"
                f"chat_id: {chat_id}\n"
                f"message_id: {message.id}\n"
                f"user_id: {sender.id}\n"
                f"username: @{sender.username or '—'}\n"
                f"reason: {verdict.reason}\n"
                f"Action: user+content blacklisted. Cannot delete/ban (not admin)."
            )
            try:
                await safe_call(
                    lambda: client.send_message("me", note), what="self_report"
                )
            except Exception:  # noqa: BLE001
                pass

        action = "reported" if reported else "flagged"
        detail = (
            f"not admin; reported to helper bot#{helper_id}: "
            f"msg_id={message.id} user_id={sender.id} ({verdict.reason})"
            if reported
            else f"not admin; blacklisted + written to Saved Messages ({verdict.reason})"
        )
        await repo.record_action(
            chat_id=chat_id, userbot_id=bot_id, spammer_id=sender.id,
            spammer_username=sender.username or "", message_id=message.id,
            action=action, stage=verdict.stage, detail=detail,
        )
        logger.info("SCAM %s in chat %s -> %s", sender.id, chat_id, action)

    async def _report_to_helper(
        self, helper_bot_id, chat_id, message_id, user_id, reason
    ) -> bool:
        """
        When the monitoring bot lacks admin rights, write the offending
        message_id + spammer user_id to the helper account, and let the helper
        enforce the ban if it is admin in the chat.
        """
        if not helper_bot_id:
            return False
        helper = self.clients.get(helper_bot_id)
        if helper is None:
            return False  # helper not running in this engine instance

        note = (
            "🚨 Anti-Spam report\n"
            f"chat_id: {chat_id}\n"
            f"message_id: {message_id}\n"
            f"user_id: {user_id}\n"
            f"reason: {reason}"
        )
        try:
            # "Write to that additional account": its own Saved Messages.
            await safe_call(lambda: helper.send_message("me", note), what="report_send")
        except Exception:  # noqa: BLE001
            pass

        # If the helper is admin in the chat, let it actually delete + ban.
        try:
            if await self._self_is_admin(helper, chat_id, helper_bot_id):
                await delete_and_ban(helper, chat_id, message_id, user_id)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _handle_count_command(self, client: Client, message, bot_id: int) -> None:
        """
        Respond to /count N in private chat.
        Returns how many messages were deleted/banned in the last N hours.
        Also works via Saved Messages: /count 20 -> "last 20 hours: X banned".
        """
        args = message.text.split()
        hours = 24  # default
        if len(args) > 1:
            try:
                hours = int(args[1])
            except ValueError:
                await message.reply(
                    "Usage: /count <hours>\nExample: /count 20"
                )
                return

        hours = max(1, min(hours, 720))  # cap at 30 days
        count = await repo.count_actions_in_hours(bot_id, hours)
        text = (
            f"📊 Anti-Spam statistics (last {hours}h):\n"
            f"• Deleted + Banned: {count['deleted_banned']}\n"
            f"• Reported to helper: {count['reported']}\n"
            f"• Flagged (AI review): {count['flagged']}\n"
            f"• Total actions: {count['total']}"
        )
        await message.reply(text)

    @staticmethod
    def _display_name(user) -> str:
        name = " ".join(
            p for p in [user.first_name or "", user.last_name or ""] if p
        )
        return name or user.username or str(user.id)

    @staticmethod
    def _aware(dt):
        if dt and djtz.is_naive(dt):
            return djtz.make_aware(dt)
        return dt

    async def _group_admins(self, client: Client, chat_id: int) -> set[int]:
        cached = self._admin_cache.get(chat_id)
        now = time.time()
        if cached and now - cached[1] < 60:
            return cached[0]
        admins: set[int] = set()
        try:
            async for m in client.get_chat_members(
                chat_id, filter=ChatMembersFilter.ADMINISTRATORS
            ):
                if m.user:
                    admins.add(m.user.id)
        except Exception:  # noqa: BLE001
            pass
        self._admin_cache[chat_id] = (admins, now)
        return admins

    async def _self_is_admin(self, client: Client, chat_id: int, bot_id: int) -> bool:
        my_id = self._self_ids.get(bot_id)
        if not my_id:
            return False
        return my_id in await self._group_admins(client, chat_id)

    async def _sender_role(self, client, chat_id, bot_id, sender) -> str:
        if getattr(sender, "is_self", False) or sender.id == self._self_ids.get(bot_id):
            return "me"
        if getattr(sender, "is_bot", False):
            return "bot"
        if getattr(sender, "is_scam", False) or getattr(sender, "is_fake", False):
            return "scam"
        if await repo.is_blacklisted(sender.id):
            return "scam"
        if sender.id in await self._group_admins(client, chat_id):
            return "admin"
        return "user"

    async def _enrich_sender(self, client: Client, user_id: int):
        """Fetch the sender's bio and download their profile photo (for the AI)."""
        bio = ""
        photo_path = None
        try:
            chat = await client.get_chat(user_id)
            bio = getattr(chat, "bio", "") or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            async for photo in client.get_chat_photos(user_id, limit=1):
                photo_path = await client.download_media(
                    photo.file_id, file_name=str(settings.MEDIA_TMP_DIR) + os.sep
                )
                break
        except Exception:  # noqa: BLE001
            pass
        return bio, photo_path

    async def _fingerprint(self, client: Client, message):
        """Return (content_type, content_hash, downloaded_media_path)."""
        if message.photo:
            path = await self._safe_download(client, message)
            return "photo", (image_phash(path) if path else None), path
        if message.animation:  # GIF
            path = await self._safe_download(client, message)
            return "gif", (image_phash(path) if path else None), path
        text = message.text or message.caption or ""
        return "text", (text_hash(text) if text.strip() else None), None

    async def _safe_download(self, client: Client, message):
        try:
            return await message.download(
                file_name=str(settings.MEDIA_TMP_DIR) + os.sep
            )
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _cleanup(path) -> None:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    async def _job_loop(self) -> None:
        """Poll for dashboard-created propagation jobs and execute them."""
        while self._running:
            try:
                jobs = await repo.claim_pending_jobs()
                for job in jobs:
                    await self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception("job loop error: %s", exc)
            await asyncio.sleep(5)

    async def _run_job(self, job: dict) -> None:
        admin_client = self.clients.get(job["admin_bot_id"])
        if admin_client is None:
            await repo.finish_job(
                job["id"],
                "failed",
                "Admin userbot is not running in the engine. Start it (it must be "
                "active) and retry.",
            )
            return
        if not job["new_bot_telegram_id"]:
            await repo.finish_job(
                job["id"], "failed", "Target userbot has no telegram_id stored."
            )
            return
        try:
            await propagate_agent(
                admin_client,
                job["chat_id"],
                job["new_bot_telegram_id"],
                title=job["admin_title"],
            )
            # Record that the new bot now monitors this chat.
            await repo.upsert_group(job["new_bot_id"], job["chat_id"], "", "")
            await repo.finish_job(
                job["id"], "done", "Invited and promoted to moderation admin."
            )
            logger.info("Propagation job #%s done.", job["id"])
        except Exception as exc:  # noqa: BLE001
            await repo.finish_job(job["id"], "failed", str(exc))
            logger.exception("Propagation job #%s failed: %s", job["id"], exc)

    async def stop(self) -> None:
        self._running = False
        if self._job_task:
            self._job_task.cancel()
        if self._cmd_task:
            self._cmd_task.cancel()
        for client in self.clients.values():
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------------- #
    #  Dashboard read commands (sync chat list / fetch history)
    # ----------------------------------------------------------------------- #
    async def _command_loop(self) -> None:
        while self._running:
            try:
                cmds = await repo.claim_pending_commands()
                for cmd in cmds:
                    await self._run_command(cmd)
            except Exception as exc:  # noqa: BLE001
                logger.exception("command loop error: %s", exc)
            await asyncio.sleep(4)

    async def _run_command(self, cmd: dict) -> None:
        client = self.clients.get(cmd["userbot_id"])
        if client is None:
            await repo.finish_command(
                cmd["id"], "failed", "Userbot is not running in the engine."
            )
            return
        try:
            if cmd["kind"] == "sync_dialogs":
                count = await self._sync_dialogs(client, cmd["userbot_id"])
                await repo.finish_command(cmd["id"], "done", f"Synced {count} chats.")
            elif cmd["kind"] == "fetch_history":
                count = await self._fetch_history(
                    client, cmd["userbot_id"], cmd["chat_id"], cmd["limit"]
                )
                await repo.finish_command(cmd["id"], "done", f"Fetched {count} messages.")
            else:
                await repo.finish_command(cmd["id"], "failed", "Unknown command.")
        except Exception as exc:  # noqa: BLE001
            await repo.finish_command(cmd["id"], "failed", str(exc))
            logger.exception("command #%s failed: %s", cmd["id"], exc)

    @staticmethod
    def _classify_chat(chat) -> str:
        t = chat.type
        if t == ChatType.PRIVATE:
            return "bot" if getattr(chat, "is_bot", False) else "private"
        if t == ChatType.BOT:
            return "bot"
        if t == ChatType.GROUP:
            return "group"
        if t == ChatType.SUPERGROUP:
            return "supergroup"
        if t == ChatType.CHANNEL:
            return "channel"
        return "group"

    async def _sync_dialogs(self, client: Client, bot_id: int, cap: int = 300) -> int:
        seen: list[int] = []
        count = 0
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            dtype = self._classify_chat(chat)

            is_admin = False
            if dtype in {"group", "supergroup", "channel"}:
                is_admin = await self._is_self_admin(client, chat.id)

            title = chat.title or " ".join(
                p for p in [getattr(chat, "first_name", ""), getattr(chat, "last_name", "")] if p
            )
            top = dialog.top_message
            preview = ""
            last_date = None
            if top:
                preview = (top.text or top.caption or "")[:120]
                last_date = djtz.make_aware(top.date) if top.date and djtz.is_naive(top.date) else top.date

            await repo.upsert_dialog(
                bot_id,
                {
                    "chat_id": chat.id,
                    "dialog_type": dtype,
                    "title": title or "",
                    "username": chat.username or "",
                    "is_admin": is_admin,
                    "members_count": getattr(chat, "members_count", None),
                    "last_message": preview,
                    "last_message_date": last_date,
                },
            )
            seen.append(chat.id)
            count += 1
            if count >= cap:
                break
        await repo.prune_dialogs(bot_id, seen)
        return count

    async def _is_self_admin(self, client: Client, chat_id: int) -> bool:
        try:
            member = await safe_call(
                lambda: client.get_chat_member(chat_id, "me"),
                what="get_chat_member",
            )
            return member.status in {
                ChatMemberStatus.OWNER,
                ChatMemberStatus.ADMINISTRATOR,
            }
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_peer(self, client: Client, chat_id: int) -> bool:
        """
        Make sure `chat_id` is resolvable.

        We load sessions from a StringSession (in-memory storage), so the peer
        cache is empty after an engine restart. When a peer is not cached,
        Pyrogram falls back to deriving it from the raw id - which fails for the
        newer, larger channel ids. Iterating get_dialogs() repopulates the cache
        (with the correct access_hash), after which resolve_peer() succeeds
        without ever hitting that fragile fallback.
        """
        try:
            await client.resolve_peer(chat_id)
            return True
        except (KeyError, ValueError):
            pass
        try:
            async for _ in client.get_dialogs():
                try:
                    await client.resolve_peer(chat_id)
                    return True
                except (KeyError, ValueError):
                    continue
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _fetch_history(
        self, client: Client, bot_id: int, chat_id: int, limit: int
    ) -> int:
        limit = max(1, min(limit or 50, 200))
        if not await self._ensure_peer(client, chat_id):
            raise ValueError(
                f"Could not resolve chat {chat_id}. Run 'Sync chat list' first so "
                "the account knows this chat."
            )
        messages: list[dict] = []
        async for m in client.get_chat_history(chat_id, limit=limit):
            sender = m.from_user
            name = ""
            if sender:
                name = " ".join(
                    p for p in [sender.first_name or "", sender.last_name or ""] if p
                ) or (sender.username or str(sender.id))
            date = m.date
            if date and djtz.is_naive(date):
                date = djtz.make_aware(date)
            messages.append(
                {
                    "message_id": m.id,
                    "sender_id": sender.id if sender else None,
                    "sender_name": name,
                    "sender_username": (sender.username if sender else "") or "",
                    "text": m.text or m.caption or "",
                    "media_type": (m.media.value if m.media else ""),
                    "outgoing": bool(m.outgoing),
                    "date": date,
                }
            )
        return await repo.replace_chat_messages(bot_id, chat_id, messages)
