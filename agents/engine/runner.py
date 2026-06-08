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

from django.conf import settings

from pyrogram import Client, filters as pf
from pyrogram.handlers import MessageHandler

from . import repository as repo
from .filters import ai_decide, precheck
from .hashing import image_phash, text_hash
from .propagation import delete_and_ban, propagate_agent

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self) -> None:
        self.clients: dict[int, Client] = {}
        self._running = False
        self._job_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        bots = await repo.list_active_userbots()
        if not bots:
            logger.warning("No active userbots found. Add one via the dashboard QR flow.")
        for bot in bots:
            await self._start_one(bot)
        # Background task that executes propagation jobs queued from the dashboard.
        self._job_task = asyncio.ensure_future(self._job_loop())
        logger.info("Agent runner online with %d userbot(s).", len(self.clients))

    async def _start_one(self, bot: dict) -> None:
        chat_ids = await repo.monitored_chat_ids(bot["id"])
        client = Client(
            name=f"agent-{bot['id']}",
            api_id=bot["api_id"] or settings.TELEGRAM_API_ID,
            api_hash=bot["api_hash"] or settings.TELEGRAM_API_HASH,
            session_string=bot["session_string"],
            workdir=str(settings.PYROGRAM_WORKDIR),
        )

        async def handler(client: Client, message, _bot_id=bot["id"], _chats=set(chat_ids)):
            try:
                if _chats and message.chat and message.chat.id not in _chats:
                    return
                await self._on_message(client, message, _bot_id)
            except Exception as exc:  # noqa: BLE001 - never kill the listener
                logger.exception("handler error: %s", exc)

        # Only react to group/supergroup messages from real users.
        client.add_handler(
            MessageHandler(handler, pf.group & ~pf.service & ~pf.me)
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
        finally:
            self._cleanup(photo_path)

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
        for client in self.clients.values():
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
