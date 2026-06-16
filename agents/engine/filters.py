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
import time
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db.models import F

from .ai_filter import classifier

# --------------------------------------------------------------------------- #
#  Stage-2 heuristics
#
#  Detection rules (keywords, emojis, regex) are managed from the dashboard and
#  stored in the `FilterRule` table. The engine caches the active rules for a
#  short TTL so edits made in the web process reach the (separate) engine
#  process within a few seconds. If the table is empty (fresh install) we fall
#  back to the built-in defaults below so protection works out of the box.
# --------------------------------------------------------------------------- #
DEFAULT_KEYWORDS = [
    # Uzbek / Russian / English bait commonly seen in adult-scam userbots.
    "profilimda", "profilim", "bio'mda", "biomda", "biomga", "sovg'a", "sovga",
    "bepul", "bosing", "havola", "kanalga", "kanalimga", "obuna",
    "intim", "yopiq kanal", "shaxsiy", "lichka", "lichkaga",
    "profile", "click here", "click", "free", "gift", "prize", "private",
    "onlyfans", "dating", "hot", "sexy", "видео", "профиль", "подпис",
    "переходи", "ссылк", "бесплатно", "интим", "эротик",
    # Telegram Premium / phishing scam keywords
    "premium", "tekin premium", "bepul premium", "premium sovg'a",
    "premium olish", "premium beradi", "premium taqdim", "aksiya",
    "получи premium", "бесплатный premium", "раздача",
    "free premium", "get premium", "claim premium", "giveaway",
    "промокод", "promo", "промо", "yutuq", "yutib ol",
]

DEFAULT_EMOJIS = ["💋", "🔞", "💦", "🍑", "🍆", "👅", "😈", "🥵", "🔥"]

# t.me / external invite links are a strong secondary signal.
DEFAULT_REGEXES = [r"(https?://|t\.me/|telegram\.me/|@[\w]{4,})"]

# Seeds used by the data migration so the rules show up (and are editable) in
# the dashboard from day one.
DEFAULT_RULE_SEEDS = (
    [("keyword", k) for k in DEFAULT_KEYWORDS]
    + [("emoji", e) for e in DEFAULT_EMOJIS]
    + [("regex", r) for r in DEFAULT_REGEXES]
)

_RULE_TTL = 15.0  # seconds
_rule_cache: dict = {
    "loaded_at": 0.0,
    "keyword_re": None,
    "phrases": [],  # normalised full-text patterns (multi-sentence)
    "emojis": [],
    "regexes": [],
}


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


@sync_to_async
def _fetch_active_rules() -> list[tuple[str, str]]:
    from agents.models import FilterRule

    return list(
        FilterRule.objects.filter(is_active=True).values_list("rule_type", "pattern")
    )


def reset_rule_cache() -> None:
    """Force the next stage-2 check to reload rules (used right after edits)."""
    _rule_cache["loaded_at"] = 0.0


async def _ensure_rules() -> None:
    now = time.time()
    if _rule_cache["loaded_at"] and now - _rule_cache["loaded_at"] < _RULE_TTL:
        return

    rows = await _fetch_active_rules()
    keywords = [p for t, p in rows if t == "keyword"]
    phrases = [p for t, p in rows if t == "phrase"]
    emojis = [p for t, p in rows if t == "emoji"]
    regexes = [p for t, p in rows if t == "regex"]

    if not rows:  # empty table -> built-in defaults
        keywords, emojis, regexes = DEFAULT_KEYWORDS, DEFAULT_EMOJIS, DEFAULT_REGEXES
        phrases = []

    _rule_cache["keyword_re"] = (
        re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE | re.DOTALL)
        if keywords
        else None
    )
    # Phrases: normalised (lowered, stripped) full-text patterns for contains check.
    _rule_cache["phrases"] = [p.lower().strip() for p in phrases if p.strip()]
    _rule_cache["emojis"] = emojis
    compiled = []
    for pat in regexes:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE | re.DOTALL))
        except re.error:
            continue  # skip an invalid pattern rather than crash the engine
    _rule_cache["regexes"] = compiled
    _rule_cache["loaded_at"] = now


async def _stage2_flag(text: str) -> tuple[bool, str]:
    await _ensure_rules()
    if not text:
        return False, ""
    reasons = []
    kre = _rule_cache["keyword_re"]
    if kre and kre.search(text):
        reasons.append("keyword")
    # Phrase match: check if any full phrase pattern is contained in the message.
    text_lower = text.lower()
    for phrase in _rule_cache["phrases"]:
        if phrase in text_lower:
            reasons.append("phrase")
            break
    if any(e in text for e in _rule_cache["emojis"]):
        reasons.append("adult-emoji")
    for rx in _rule_cache["regexes"]:
        if rx.search(text):
            reasons.append("regex")
            break
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
    suspicious, why = await _stage2_flag(text)
    if not suspicious:
        return Verdict(False, "clean")

    return Verdict(False, "suspicious", reason=f"stage2: {why}", needs_ai=True)


async def ai_decide(*, text: str, bio: str = "", photo_path: str | None = None,
                    stage2_reason: str = "") -> Verdict:
    """
    Stage 3 - call the AI with the enriched context.

    If the AI classifier is disabled (no API key), fall back to heuristic:
    - If stage-2 found MULTIPLE signals (keyword+emoji, keyword+regex, etc.) -> SPAM
    - If stage-2 found only one weak signal -> SAFE (avoid false positives)
    """
    clf = classifier()

    if not clf.enabled:
        # AI not available — use heuristic fallback based on stage-2 signals.
        # Multiple distinct signals = high confidence of spam.
        signals = [s.strip() for s in stage2_reason.replace("stage2:", "").split(",") if s.strip()]
        if len(signals) >= 2:
            # Two or more independent signals = treat as spam
            return Verdict(
                True, "heuristic",
                reason=f"AI disabled, multi-signal heuristic ({stage2_reason})",
                should_cache=True,
            )
        # Single signal with keyword match AND link in text = very likely spam
        if "keyword" in signals and ("t.me/" in text or "http" in text.lower()):
            return Verdict(
                True, "heuristic",
                reason=f"AI disabled, keyword+link heuristic ({stage2_reason})",
                should_cache=True,
            )
        # Single signal only = not enough confidence, let it pass
        return Verdict(False, "heuristic", reason=f"AI disabled, weak signal ({stage2_reason})")

    verdict_word = await clf.classify(text=text, bio=bio, photo_path=photo_path)
    if verdict_word == "SPAM_BOT":
        return Verdict(
            True, "ai", reason=f"AI=SPAM_BOT ({stage2_reason})", should_cache=True
        )
    return Verdict(False, "ai", reason=f"AI=SAFE ({stage2_reason})")
