"""
Propagate a newly-linked userbot into a group using an existing admin userbot.

Usage:
    python manage.py propagate_agent --admin <UserBot id> --new <UserBot id> --chat <chat_id>

"UserBot A" (admin) invites "UserBot B" (new) into the chat and promotes it to a
moderation admin (delete messages + ban users). FloodWait-safe.
"""
import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pyrogram import Client

from agents.engine.propagation import propagate_agent
from agents.models import UserBot


class Command(BaseCommand):
    help = "Invite + promote a userbot into a group via an existing admin userbot."

    def add_arguments(self, parser):
        parser.add_argument("--admin", type=int, required=True, help="Admin UserBot id")
        parser.add_argument("--new", type=int, required=True, help="New UserBot id")
        parser.add_argument("--chat", type=int, required=True, help="Target chat id")

    def handle(self, *args, **opts):
        try:
            admin_bot = UserBot.objects.get(pk=opts["admin"])
            new_bot = UserBot.objects.get(pk=opts["new"])
        except UserBot.DoesNotExist as exc:
            raise CommandError(str(exc))

        if not new_bot.telegram_id:
            raise CommandError("New userbot has no telegram_id stored.")

        asyncio.run(self._run(admin_bot, new_bot, opts["chat"]))
        self.stdout.write(self.style.SUCCESS("Propagation complete."))

    async def _run(self, admin_bot: UserBot, new_bot: UserBot, chat_id: int):
        client = Client(
            name=f"admin-{admin_bot.id}",
            api_id=admin_bot.api_id or settings.TELEGRAM_API_ID,
            api_hash=admin_bot.api_hash or settings.TELEGRAM_API_HASH,
            session_string=admin_bot.session_string,
            in_memory=True,
        )
        async with client:
            await propagate_agent(client, chat_id, new_bot.telegram_id)
