"""
QR-code login for new userbots (the most important part of the platform).

Because Telegram now heavily throttles SMS codes, the only reliable way to add
new userbots at scale is the official "Link Desktop Device" QR flow. This module
implements that flow purely with Pyrogram's raw MTProto layer:

    1.  Connect an *unauthorised* Pyrogram client (in-memory session).
    2.  Call `auth.ExportLoginToken` -> receive a login token.
    3.  Encode the token into a `tg://login?token=...` URL and render a QR image.
    4.  Keep calling `ExportLoginToken`:
          * the same token is returned until it expires (~30s) -> refresh QR,
          * `LoginTokenMigrateTo` -> migrate to the target DC + ImportLoginToken,
          * `LoginTokenSuccess` -> the user has scanned & confirmed: we are in!
    5.  Export a portable StringSession and persist it as an `active` UserBot.

Everything runs on the shared background event loop (see `loop.py`) so a single
QR client survives across the many AJAX polling requests coming from the browser.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import uuid
from dataclasses import dataclass, field

import qrcode
from asgiref.sync import sync_to_async
from django.conf import settings

from pyrogram import Client
from pyrogram.errors import PasswordHashInvalid, SessionPasswordNeeded
from pyrogram.raw.functions.auth import ExportLoginToken, ImportLoginToken
from pyrogram.raw.types.auth import (
    LoginToken,
    LoginTokenMigrateTo,
    LoginTokenSuccess,
)
from pyrogram.session import Auth, Session

from .loop import get_loop

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Per-session state shared with the dashboard via polling
# --------------------------------------------------------------------------- #
@dataclass
class QRState:
    session_id: str
    status: str = "pending"  # pending | waiting | password | success | error | expired
    qr_png_b64: str = ""     # data ready to drop into <img src="data:image/png;base64,...">
    login_url: str = ""
    expires_at: float = 0.0
    error: str = ""
    userbot_id: int | None = None
    username: str = ""
    # --- 2FA (cloud password) support ---
    password: str = ""
    password_error: str = ""
    password_event: object = None  # asyncio.Event, created on the engine loop
    created_at: float = field(default_factory=time.time)


def _make_qr_png_b64(data: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _token_to_url(token: bytes) -> str:
    b64 = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    return f"tg://login?token={b64}"


class QRLoginManager:
    """Singleton orchestrating all in-flight QR-login sessions."""

    _instance: "QRLoginManager | None" = None

    def __init__(self) -> None:
        self._states: dict[str, QRState] = {}
        self._loop = get_loop()

    @classmethod
    def instance(cls) -> "QRLoginManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ----- public, synchronous API used by Django views ------------------- #
    def start(self, api_id: int | None = None, api_hash: str | None = None) -> QRState:
        session_id = uuid.uuid4().hex
        state = QRState(session_id=session_id)
        self._states[session_id] = state
        # Fire-and-forget the long-running login coroutine on the bg loop.
        self._loop.submit(self._run_login(state, api_id, api_hash))
        return state

    def get(self, session_id: str) -> QRState | None:
        return self._states.get(session_id)

    def submit_password(self, session_id: str, password: str) -> bool:
        """Called by the dashboard when the operator submits the 2FA password."""
        state = self._states.get(session_id)
        if not state or state.password_event is None:
            return False
        state.password = password
        state.password_error = ""
        # The event lives on the background loop; signal it thread-safely.
        self._loop.loop.call_soon_threadsafe(state.password_event.set)
        return True

    def cleanup(self, max_age: float = 600) -> None:
        now = time.time()
        for sid in list(self._states):
            if now - self._states[sid].created_at > max_age:
                self._states.pop(sid, None)

    # ----- the actual async flow ------------------------------------------ #
    async def _run_login(self, state: QRState, api_id, api_hash) -> None:
        api_id = api_id or settings.TELEGRAM_API_ID
        api_hash = api_hash or settings.TELEGRAM_API_HASH
        if not api_id or not api_hash:
            state.status = "error"
            state.error = "TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured."
            return

        client = Client(
            name=f"qr-{state.session_id}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
        )
        # The Event must be created on the loop that will await it (this loop).
        state.password_event = asyncio.Event()

        try:
            await client.connect()  # connect WITHOUT authorising
            await self._login_loop(client, state, api_id, api_hash)
        except Exception as exc:  # noqa: BLE001 - surface any failure to UI
            logger.exception("QR login failed: %s", exc)
            state.status = "error"
            state.error = str(exc)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _login_loop(self, client: Client, state: QRState, api_id, api_hash) -> None:
        deadline = time.time() + 300  # give the operator 5 minutes to scan
        while time.time() < deadline:
            try:
                r = await client.invoke(
                    ExportLoginToken(api_id=api_id, api_hash=api_hash, except_ids=[])
                )
            except SessionPasswordNeeded:
                # The account has 2FA (cloud password) enabled. The QR scan was
                # accepted but Telegram now needs the password to finish.
                await self._handle_2fa(client, state)
                return

            if isinstance(r, LoginToken):
                # Still waiting for a scan -> (re)publish the QR for the browser.
                state.login_url = _token_to_url(r.token)
                state.qr_png_b64 = _make_qr_png_b64(state.login_url)
                state.expires_at = float(r.expires)
                state.status = "waiting"
                # Poll again shortly; refresh well before the ~30s expiry.
                await asyncio.sleep(3)
                continue

            if isinstance(r, LoginTokenMigrateTo):
                try:
                    r = await self._migrate(client, r.dc_id, r.token)
                except SessionPasswordNeeded:
                    await self._handle_2fa(client, state)
                    return

            if isinstance(r, LoginTokenSuccess):
                await self._finalise(client, state)
                return

        state.status = "expired"
        state.error = "QR code expired before it was scanned."

    async def _handle_2fa(self, client: Client, state: QRState) -> None:
        """Prompt the dashboard for the cloud password and complete sign-in."""
        while True:
            state.status = "password"
            try:
                await asyncio.wait_for(state.password_event.wait(), timeout=180)
            except asyncio.TimeoutError:
                state.status = "expired"
                state.error = "2FA password was not provided in time."
                return
            state.password_event.clear()

            try:
                await client.check_password(state.password)
            except PasswordHashInvalid:
                # Let the operator try again with a different password.
                state.password_error = "Incorrect password. Please try again."
                continue
            finally:
                state.password = ""  # never keep the plaintext password around

            await self._finalise(client, state)
            return

    async def _migrate(self, client: Client, dc_id: int, token: bytes):
        """Handle `LoginTokenMigrateTo` by moving the session to the target DC."""
        await client.session.stop()
        await client.storage.dc_id(dc_id)
        await client.storage.auth_key(
            await Auth(client, dc_id, await client.storage.test_mode()).create()
        )
        client.session = Session(
            client,
            dc_id,
            await client.storage.auth_key(),
            await client.storage.test_mode(),
        )
        await client.session.start()
        return await client.invoke(ImportLoginToken(token=token))

    async def _finalise(self, client: Client, state: QRState) -> None:
        """A scan succeeded: persist the session string as an active UserBot."""
        me = await client.get_me()
        session_string = await client.export_session_string()

        from agents.models import UserBot

        userbot = await sync_to_async(UserBot.objects.create)(
            session_string=session_string,
            phone=me.phone_number or "",
            username=me.username or "",
            telegram_id=me.id,
            api_id=client.api_id,
            api_hash=client.api_hash,
            status=UserBot.Status.ACTIVE,
        )

        state.status = "success"
        state.userbot_id = userbot.pk
        state.username = me.username or me.first_name or str(me.id)
        logger.info("QR login complete -> UserBot #%s (%s)", userbot.pk, state.username)


def manager() -> QRLoginManager:
    return QRLoginManager.instance()
