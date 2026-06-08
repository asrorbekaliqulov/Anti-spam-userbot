"""
The hybrid filter pipeline.

Every incoming group message is run through three increasingly expensive stages:

    Stage 1  (instant DB cache)  - sender on the blacklist OR message fingerprint
                                   already known as spam  ->  delete + ban.
    Stage 2  (regex / emoji)     - suspicious keywords or adult emojis present?
                                   If not -> clean. If yes -> escalate to stage 3.
    Stage 3  (ChatGPT 4o-mini)   - ambiguous case sent to the model. A 'SPAM_BOT'
                                   verdict -> ban + blacklist + cache the payload
                                   fingerprint so it is blocked instantly next time.

`inspect_message` is pure decision logic; it performs the read-only DB lookups
and the AI call, then returns a `Verdict`. The agent runner is responsible for
carrying out the Telegram action (delete/ban) and writing the SecurityLog.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db.models import F

from .ai_filter import classifier

# --------------------------------------------------------------------------- #
#  Stage-2 heuristics
# --------------------------------------------------------------------------- #
SUSPICIOUS_KEYWORDS = [
    # Uzbek / Russian / English bait commonly seen in adult-scam userbots.
    "profilimda", "profilim", "bio'mda", "biomda", "biomga", "sovg'a", "sovga",
    "bepul", "bosing", "havola", "kanalga", "kanalimga", "obuna",
    "intim", "yopiq kanal", "shaxsiy", "lichka", "lichkaga",
    "profile", "click here", "click", "free", "gift", "prize", "private",
    "onlyfans", "dating", "hot", "sexy", "видео", "профиль", "подпис",
    "переходи", "ссылк", "бесплатно", "интим", "эротик",
]

ADULT_EMOJIS = ["💋", "🔞", "💦", "🍑", "🍆", "👅", "😈", "🥵", "🔥"]

_KEYWORD_RE = re.compile(
    "|".join(re.escape(k) for k in SUSPICIOUS_KEYWORDS), re.IGNORECASE
)
# t.me / external invite links are a strong secondary signal.
_LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|@[\w]{4,})", re.IGNORECASE)


@dataclass
class Verdict:
    is_spam: bool
    stage: str            # blacklist | spam_cache | regex | ai | clean | suspicious
    reason: str = ""
    should_cache: bool = False  # cache the fingerprint (AI-confirmed novel spam)
    needs_ai: bool = False      # stage-2 was inconclusive -> escalate to stage 3


# --------------------------------------------------------------------------- #
#  Read-only DB helpers (async-safe)
# --------------------------------------------------------------------------- #
@sync_to_async
def _is_blacklisted(telegram_id: int) -> bool:
    from agents.models import BlacklistUser

    return BlacklistUser.objects.filter(telegram_id=telegram_id).exists()


@sync_to_async
def _spam_content_hit(content_type: str, content_hash: str) -> bool:
    from agents.models import SpamContent

    qs = SpamContent.objects.filter(content_type=content_type, content_hash=content_hash)
    if qs.exists():
        qs.update(hits=F("hits") + 1)
        return True
    return False


def _stage2_flag(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    reasons = []
    if _KEYWORD_RE.search(text):
        reasons.append("keyword")
    if any(e in text for e in ADULT_EMOJIS):
        reasons.append("adult-emoji")
    if _LINK_RE.search(text):
        reasons.append("link/mention")
    return (bool(reasons), ",".join(reasons))


# --------------------------------------------------------------------------- #
#  Public entry points
# --------------------------------------------------------------------------- #
async def precheck(
    *,
    text: str,
    sender_id: int,
    content_type: str = "text",
    content_hash: str | None = None,
) -> Verdict:
    """
    Cheap stages 1 & 2 only. Never calls the network/AI.

    Returns a final verdict for clean/blacklist/spam_cache messages, or a
    `needs_ai=True` verdict telling the caller to gather bio/photo and escalate.
    """
    # --- Stage 1: blacklisted sender -------------------------------------- #
    if await _is_blacklisted(sender_id):
        return Verdict(True, "blacklist", reason="sender on blacklist")

    # --- Stage 1: known spam fingerprint ---------------------------------- #
    if content_hash and await _spam_content_hit(content_type, content_hash):
        return Verdict(True, "spam_cache", reason=f"known {content_type} fingerprint")

    # --- Stage 2: regex / emoji heuristics -------------------------------- #
    suspicious, why = _stage2_flag(text)
    if not suspicious:
        return Verdict(False, "clean")

    return Verdict(False, "suspicious", reason=f"stage2: {why}", needs_ai=True)


async def ai_decide(*, text: str, bio: str = "", photo_path: str | None = None,
                    stage2_reason: str = "") -> Verdict:
    """Stage 3 only - call the AI with the enriched context."""
    verdict_word = await classifier().classify(text=text, bio=bio, photo_path=photo_path)
    if verdict_word == "SPAM_BOT":
        return Verdict(
            True, "ai", reason=f"AI=SPAM_BOT ({stage2_reason})", should_cache=True
        )
    return Verdict(False, "ai", reason=f"AI=SAFE ({stage2_reason})")
