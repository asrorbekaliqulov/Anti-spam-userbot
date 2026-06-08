"""
Group propagation & automatic admin promotion.

Scenario: "UserBot A" is already an admin of a group. We want to bring a freshly
linked "UserBot B" into the same group and grant it the moderation rights it
needs (delete messages + ban users) so it can help police the chat.

Flow (all driven from the dashboard, fully automated):

    1.  A invites B to the group  (add_chat_members).
    2.  A waits a throttled moment, then promotes B to admin with a restricted
        rights set (delete messages + ban users only).

Every Telegram call is wrapped so a `FloodWait` is respected (we sleep for the
server-requested duration and retry) and a base throttle (`ACTION_THROTTLE_SECONDS`)
is inserted between sensitive operations to stay comfortably under the limits.
"""
from __future__ import annotations

import asyncio
import logging

from django.conf import settings

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import ChatPrivileges

logger = logging.getLogger(__name__)


async def throttle() -> None:
    await asyncio.sleep(settings.ACTION_THROTTLE_SECONDS)


async def safe_call(coro_factory, *, retries: int = 3, what: str = "telegram call"):
    """
    Execute an awaitable produced by `coro_factory`, transparently honouring
    FloodWait. `coro_factory` must be a zero-arg callable returning a fresh
    coroutine each time (so we can retry).
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except FloodWait as e:
            wait = int(getattr(e, "value", 0)) + 1
            logger.warning("FloodWait on %s: sleeping %ss", what, wait)
            await asyncio.sleep(wait)
            attempt += 1
            if attempt > retries:
                raise


# --------------------------------------------------------------------------- #
#  Moderation rights granted to a worker bot
# --------------------------------------------------------------------------- #
MODERATION_RIGHTS = ChatPrivileges(
    can_delete_messages=True,
    can_restrict_members=True,   # ban / restrict users
    can_manage_chat=True,
    can_invite_users=True,
    can_promote_members=False,
    can_change_info=False,
    can_pin_messages=False,
)


async def invite_bot_to_group(
    admin_client: Client,
    chat_id: int,
    new_bot_user_id: int,
) -> None:
    """UserBot A adds UserBot B (by user id) to the group."""
    await safe_call(
        lambda: admin_client.add_chat_members(chat_id, new_bot_user_id),
        what="add_chat_members",
    )
    await throttle()


async def promote_bot_as_admin(
    admin_client: Client,
    chat_id: int,
    new_bot_user_id: int,
    title: str = "Anti-Spam Agent",
) -> None:
    """UserBot A promotes UserBot B to a restricted moderation admin."""
    await safe_call(
        lambda: admin_client.promote_chat_member(
            chat_id, new_bot_user_id, privileges=MODERATION_RIGHTS
        ),
        what="promote_chat_member",
    )
    await throttle()
    # Optional cosmetic admin title (ignore failures - not all chats allow it).
    try:
        await safe_call(
            lambda: admin_client.set_administrator_title(
                chat_id, new_bot_user_id, title
            ),
            what="set_administrator_title",
        )
    except Exception:  # noqa: BLE001
        pass


async def propagate_agent(
    admin_client: Client,
    chat_id: int,
    new_bot_user_id: int,
    title: str = "Anti-Spam Agent",
) -> None:
    """Full pipeline: invite B then promote B to a moderation admin."""
    await invite_bot_to_group(admin_client, chat_id, new_bot_user_id)
    await promote_bot_as_admin(admin_client, chat_id, new_bot_user_id, title=title)
    logger.info("Propagated agent %s into chat %s as admin", new_bot_user_id, chat_id)


# --------------------------------------------------------------------------- #
#  Enforcement primitives used by the agent runner
# --------------------------------------------------------------------------- #
async def delete_and_ban(
    client: Client,
    chat_id: int,
    message_id: int,
    user_id: int,
) -> None:
    """Delete the offending message and ban its sender (FloodWait-safe)."""
    await safe_call(
        lambda: client.delete_messages(chat_id, message_id),
        what="delete_messages",
    )
    await throttle()
    await safe_call(
        lambda: client.ban_chat_member(chat_id, user_id),
        what="ban_chat_member",
    )
