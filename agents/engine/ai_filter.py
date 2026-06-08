"""
Stage-3 classifier: ChatGPT (gpt-4o-mini).

Only reached when the cheap stages (DB cache + regex/emoji) flag a message as
*suspicious* but cannot prove it. The model is given the message text, the
sender's bio and (optionally) the profile photo, and must answer with exactly
one word: SPAM_BOT or SAFE.
"""
from __future__ import annotations

import base64
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Telegram anti-spam classifier specialised in 'adult/scam' and "
    "phishing userbots. You receive a chat message together with the sender's "
    "profile bio and optionally their profile picture. Decide whether the sender "
    "is an automated spam/scam account. Typical signals: invitations to view a "
    "private profile, adult content bait, 'gifts'/'prizes', links to external "
    "channels, instructions like 'click here' / 'see my bio'. "
    "Answer with EXACTLY one token and nothing else: 'SPAM_BOT' or 'SAFE'."
)


class AIClassifier:
    def __init__(self) -> None:
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(settings.OPENAI_API_KEY)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def classify(
        self,
        text: str,
        bio: str = "",
        photo_path: str | None = None,
    ) -> str:
        """Return 'SPAM_BOT' or 'SAFE' (defaults to 'SAFE' on any error)."""
        if not self.enabled:
            return "SAFE"

        user_content: list[dict] = [
            {
                "type": "text",
                "text": (
                    f"MESSAGE:\n{text or '(no text)'}\n\n"
                    f"SENDER BIO:\n{bio or '(empty)'}"
                ),
            }
        ]

        if photo_path:
            try:
                with open(photo_path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    }
                )
            except OSError:
                pass

        try:
            client = self._get_client()
            resp = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                max_tokens=4,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            verdict = (resp.choices[0].message.content or "").strip().upper()
            return "SPAM_BOT" if "SPAM_BOT" in verdict else "SAFE"
        except Exception as exc:  # noqa: BLE001 - never crash the agent over AI
            logger.warning("AI classification failed: %s", exc)
            return "SAFE"


_classifier: AIClassifier | None = None


def classifier() -> AIClassifier:
    global _classifier
    if _classifier is None:
        _classifier = AIClassifier()
    return _classifier
