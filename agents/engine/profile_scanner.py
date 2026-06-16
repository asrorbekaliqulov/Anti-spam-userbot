"""
Profile scanner: checks new group members for NSFW/18+ content.

When a user joins a monitored group the engine:

    1. Downloads their profile photo(s) and runs NSFW classification.
    2. Fetches the user's bio/description looking for linked channels.
    3. If a linked channel is found, fetches recent posts and checks for
       adult imagery.

Classification uses the OpenAI vision model (same as the AI filter stage 3)
to decide if a profile photo or channel content is NSFW/adult. This keeps the
dependency set small (no extra nudity-detection libraries needed).

If NSFW content is detected the scanner returns a result indicating what was
found so the runner can ban + remove the user immediately.
"""
from __future__ import annotations

import base64
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# Regex to extract @username or t.me/username links from bio text.
_CHANNEL_RE = re.compile(
    r"(?:@([a-zA-Z]\w{3,30})|(?:https?://)?t\.me/([a-zA-Z]\w{3,30}))"
)

NSFW_SYSTEM_PROMPT = (
    "You are a content safety classifier. You will be shown an image. "
    "Determine if it contains nudity, sexually explicit content, pornography, "
    "or adult/18+ material. Look for: exposed genitalia, sexual acts, "
    "provocative nudity, adult content watermarks, or OnlyFans-style content. "
    "Answer with EXACTLY one word: 'NSFW' if it contains adult/18+ content, "
    "or 'SAFE' if it does not. Nothing else."
)


class ProfileScanner:
    """Scans user profiles and linked channels for NSFW content."""

    def __init__(self) -> None:
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(settings.OPENAI_API_KEY)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            try:
                self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            except TypeError:
                import httpx
                self._client = AsyncOpenAI(
                    api_key=settings.OPENAI_API_KEY,
                    http_client=httpx.AsyncClient(),
                )
        return self._client

    async def classify_image(self, photo_path: str) -> str:
        """
        Classify a single image file as 'NSFW' or 'SAFE'.
        Returns 'SAFE' on any error (fail-open to avoid false bans).
        """
        if not self.enabled or not photo_path:
            return "SAFE"

        try:
            with open(photo_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return "SAFE"

        try:
            client = self._get_client()
            resp = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                max_tokens=4,
                messages=[
                    {"role": "system", "content": NSFW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64}"
                                },
                            }
                        ],
                    },
                ],
            )
            verdict = (resp.choices[0].message.content or "").strip().upper()
            return "NSFW" if "NSFW" in verdict else "SAFE"
        except Exception as exc:  # noqa: BLE001
            logger.warning("NSFW classification failed: %s", exc)
            return "SAFE"

    async def scan_profile_photos(
        self, pyrogram_client, user_id: int, limit: int = 3
    ) -> tuple[bool, str]:
        """
        Download and scan the user's profile photos.

        Returns:
            (is_nsfw: bool, detail: str)
        """
        if not self.enabled:
            return False, "AI not configured"

        photos_checked = 0
        try:
            async for photo in pyrogram_client.get_chat_photos(user_id, limit=limit):
                photo_path = await pyrogram_client.download_media(
                    photo.file_id,
                    file_name=str(settings.MEDIA_TMP_DIR) + os.sep,
                )
                if not photo_path:
                    continue

                try:
                    verdict = await self.classify_image(photo_path)
                    photos_checked += 1
                    if verdict == "NSFW":
                        return True, f"NSFW profile photo #{photos_checked}"
                finally:
                    _cleanup(photo_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error scanning profile photos for %s: %s", user_id, exc)

        return False, f"Checked {photos_checked} photos - all clean"

    async def scan_linked_channel(
        self, pyrogram_client, user_id: int, bio: str = ""
    ) -> tuple[bool, str]:
        """
        Check if user's bio contains a channel link, and if that channel
        has NSFW content in recent posts.

        Returns:
            (is_nsfw: bool, detail: str)
        """
        if not self.enabled:
            return False, "AI not configured"

        # Get bio if not provided
        if not bio:
            try:
                chat = await pyrogram_client.get_chat(user_id)
                bio = getattr(chat, "bio", "") or ""
            except Exception:  # noqa: BLE001
                return False, "Could not fetch bio"

        if not bio:
            return False, "No bio"

        # Extract channel/username links from bio
        matches = _CHANNEL_RE.findall(bio)
        usernames = [m[0] or m[1] for m in matches if m[0] or m[1]]

        if not usernames:
            return False, "No channel links in bio"

        # Check each linked channel (max 2 to avoid flooding)
        for username in usernames[:2]:
            try:
                is_nsfw, detail = await self._check_channel(
                    pyrogram_client, username
                )
                if is_nsfw:
                    return True, f"NSFW content in @{username}: {detail}"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error checking channel @%s: %s", username, exc)
                continue

        return False, f"Checked {len(usernames[:2])} channel(s) - clean"

    async def _check_channel(
        self, pyrogram_client, username: str
    ) -> tuple[bool, str]:
        """
        Fetch recent posts from a channel and scan images for NSFW content.
        Checks up to 10 recent messages with photos.
        """
        photos_scanned = 0
        try:
            async for msg in pyrogram_client.get_chat_history(
                f"@{username}", limit=20
            ):
                if not msg.photo:
                    continue

                photo_path = None
                try:
                    photo_path = await msg.download(
                        file_name=str(settings.MEDIA_TMP_DIR) + os.sep
                    )
                    if not photo_path:
                        continue

                    verdict = await self.classify_image(photo_path)
                    photos_scanned += 1
                    if verdict == "NSFW":
                        return True, f"NSFW post found (checked {photos_scanned} images)"
                finally:
                    _cleanup(photo_path)

                if photos_scanned >= 5:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cannot access channel @%s: %s", username, exc)

        return False, f"Checked {photos_scanned} images"

    async def full_scan(
        self, pyrogram_client, user_id: int
    ) -> tuple[str, str]:
        """
        Run a full profile scan (photos + linked channels).

        Returns:
            (scan_result: str, detail: str)
            scan_result is one of: clean, nsfw_photo, nsfw_channel, nsfw_both, scan_failed
        """
        photo_nsfw = False
        channel_nsfw = False
        details = []

        # Scan profile photos
        try:
            photo_nsfw, photo_detail = await self.scan_profile_photos(
                pyrogram_client, user_id
            )
            details.append(f"Photos: {photo_detail}")
        except Exception as exc:  # noqa: BLE001
            details.append(f"Photo scan error: {exc}")

        # Get bio for channel scanning
        bio = ""
        try:
            chat = await pyrogram_client.get_chat(user_id)
            bio = getattr(chat, "bio", "") or ""
        except Exception:  # noqa: BLE001
            pass

        # Scan linked channels
        try:
            channel_nsfw, channel_detail = await self.scan_linked_channel(
                pyrogram_client, user_id, bio=bio
            )
            details.append(f"Channel: {channel_detail}")
        except Exception as exc:  # noqa: BLE001
            details.append(f"Channel scan error: {exc}")

        # Determine overall result
        if photo_nsfw and channel_nsfw:
            scan_result = "nsfw_both"
        elif photo_nsfw:
            scan_result = "nsfw_photo"
        elif channel_nsfw:
            scan_result = "nsfw_channel"
        else:
            scan_result = "clean"

        return scan_result, " | ".join(details)


def _cleanup(path) -> None:
    """Remove a temporary file."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# Singleton instance
_scanner: ProfileScanner | None = None


def scanner() -> ProfileScanner:
    global _scanner
    if _scanner is None:
        _scanner = ProfileScanner()
    return _scanner
