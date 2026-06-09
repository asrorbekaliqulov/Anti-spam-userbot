"""
Database architecture for the Anti-Spam Agent Platform.

The DB is the single source of truth shared between the synchronous Django
dashboard and the asynchronous Pyrogram engine. The engine reads configuration
(which userbots are active, which groups to monitor, the blacklist/spam cache)
and writes back results (new sessions, security logs, freshly detected spam).
"""
from django.db import models
from django.utils import timezone


class UserBot(models.Model):
    """A single Telegram userbot ("Agent") controlled by the platform."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISCONNECTED = "disconnected", "Disconnected"
        CONNECTING = "connecting", "Connecting"

    # Pyrogram StringSession produced after a successful QR login.
    session_string = models.TextField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    username = models.CharField(max_length=64, blank=True, default="")
    telegram_id = models.BigIntegerField(null=True, blank=True)

    # Per-bot API credentials (fall back to settings defaults when empty).
    api_id = models.IntegerField(null=True, blank=True)
    api_hash = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CONNECTING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.username or self.phone or f"bot#{self.pk}"
        return f"{label} ({self.status})"

    @property
    def is_ready(self) -> bool:
        return bool(self.session_string) and self.status == self.Status.ACTIVE


class TelegramGroup(models.Model):
    """A group/supergroup that a userbot is monitoring."""

    chat_id = models.BigIntegerField()
    title = models.CharField(max_length=255, blank=True, default="")
    invite_link = models.CharField(max_length=255, blank=True, default="")
    monitored_by = models.ForeignKey(
        UserBot,
        on_delete=models.CASCADE,
        related_name="groups",
    )
    # Whether the monitoring userbot itself has admin rights in this group.
    monitor_is_admin = models.BooleanField(default=False)
    # Optional secondary account used when the monitoring bot is NOT admin:
    # it receives the spam message_id + spammer id (and can enforce if it is
    # admin and running in the engine).
    helper_bot = models.ForeignKey(
        UserBot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="helper_for_groups",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["chat_id", "monitored_by"],
                name="unique_group_per_bot",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title or self.chat_id}"


class BlacklistUser(models.Model):
    """Known spammer accounts. Stage-1 fast lookup keys off telegram_id."""

    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    username = models.CharField(max_length=64, blank=True, default="")
    first_name = models.CharField(max_length=128, blank=True, default="")
    bio = models.TextField(blank=True, default="")
    reason_text = models.CharField(max_length=255, blank=True, default="")
    detected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-detected_at"]

    def __str__(self) -> str:
        return f"{self.username or self.first_name or self.telegram_id}"


class SpamContent(models.Model):
    """
    Fingerprints of known spam payloads.

    For text we store an MD5 of the normalised message; for images/GIFs we store
    a perceptual hash (pHash) so visually-identical media is matched even after
    re-compression.
    """

    class ContentType(models.TextChoices):
        TEXT = "text", "Text"
        GIF = "gif", "GIF"
        PHOTO = "photo", "Photo"

    content_type = models.CharField(max_length=8, choices=ContentType.choices)
    # MD5 (text) or pHash (media). Indexed + unique for O(1) fast-path lookups.
    content_hash = models.CharField(max_length=64, db_index=True)
    raw_data = models.TextField(
        blank=True,
        default="",
        help_text="Original text, or a short descriptor for media.",
    )
    hits = models.PositiveIntegerField(default=0)
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "content_hash"],
                name="unique_content_fingerprint",
            )
        ]

    def __str__(self) -> str:
        return f"{self.content_type}:{self.content_hash[:12]}"


class FilterRule(models.Model):
    """
    A dashboard-managed stage-2 detection rule.

    Keywords (substring match), single emojis, or full regex patterns can be
    added/removed/toggled from the UI. The engine reloads active rules on a short
    TTL so changes take effect within seconds across processes.
    """

    class RuleType(models.TextChoices):
        KEYWORD = "keyword", "Keyword (substring)"
        PHRASE = "phrase", "Phrase (full text match)"
        EMOJI = "emoji", "Emoji"
        REGEX = "regex", "Regex pattern"

    rule_type = models.CharField(max_length=16, choices=RuleType.choices)
    pattern = models.CharField(max_length=255)
    note = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    hits = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["rule_type", "pattern"]
        constraints = [
            models.UniqueConstraint(
                fields=["rule_type", "pattern"], name="unique_filter_rule"
            )
        ]

    def __str__(self) -> str:
        return f"[{self.rule_type}] {self.pattern}"


class PropagationJob(models.Model):
    """
    A request (created from the dashboard) for an admin userbot to invite and
    promote another userbot inside a group.

    The engine process picks up `pending` jobs and runs them with its already
    connected admin session - this avoids using the same session string from two
    processes at once (which Telegram can treat as a hijack and revoke).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    admin_bot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="propagation_jobs_as_admin"
    )
    new_bot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="propagation_jobs_as_target"
    )
    chat_id = models.BigIntegerField()
    admin_title = models.CharField(max_length=64, blank=True, default="Anti-Spam Agent")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    result = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"propagate bot#{self.new_bot_id} -> chat {self.chat_id} ({self.status})"


class SecurityLog(models.Model):
    """Audit trail of every protective action taken by an agent."""

    class Action(models.TextChoices):
        DELETED = "deleted", "Message deleted"
        BANNED = "banned", "User banned"
        DELETED_AND_BANNED = "deleted_banned", "Deleted + Banned"
        FLAGGED = "flagged", "Flagged (AI)"
        REPORTED = "reported", "Reported to helper account"

    group = models.ForeignKey(
        TelegramGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs",
    )
    handled_by = models.ForeignKey(
        UserBot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs",
    )
    spammer_id = models.BigIntegerField(null=True, blank=True)
    spammer_username = models.CharField(max_length=64, blank=True, default="")
    message_id = models.BigIntegerField(null=True, blank=True)
    action_taken = models.CharField(max_length=24, choices=Action.choices)
    # Which pipeline stage triggered the action (blacklist / regex / ai).
    stage = models.CharField(max_length=32, blank=True, default="")
    detail = models.CharField(max_length=512, blank=True, default="")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.action_taken} {self.spammer_id} @ {self.timestamp:%Y-%m-%d %H:%M}"



class TelegramDialog(models.Model):
    """
    A cached entry from a userbot's chat list (the result of `get_dialogs`).

    This powers the Telegram-like browser in the dashboard. It is a *cache*: the
    engine refreshes it on demand (via an EngineCommand) because only the engine
    process owns the live Pyrogram session.
    """

    class DialogType(models.TextChoices):
        PRIVATE = "private", "User"
        BOT = "bot", "Bot"
        GROUP = "group", "Group"
        SUPERGROUP = "supergroup", "Supergroup"
        CHANNEL = "channel", "Channel"

    userbot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="dialogs"
    )
    chat_id = models.BigIntegerField()
    dialog_type = models.CharField(max_length=12, choices=DialogType.choices)
    title = models.CharField(max_length=255, blank=True, default="")
    username = models.CharField(max_length=64, blank=True, default="")
    is_admin = models.BooleanField(default=False)
    members_count = models.IntegerField(null=True, blank=True)
    # Mirror of "is this group being anti-spam monitored" for quick display.
    monitored = models.BooleanField(default=False)
    last_message = models.CharField(max_length=512, blank=True, default="")
    last_message_date = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_date", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["userbot", "chat_id"], name="unique_dialog_per_bot"
            )
        ]

    def __str__(self) -> str:
        return f"{self.title or self.username or self.chat_id} ({self.dialog_type})"

    @property
    def is_group(self) -> bool:
        return self.dialog_type in {self.DialogType.GROUP, self.DialogType.SUPERGROUP}


class ChatMessage(models.Model):
    """A cached message used to read a chat's recent history in the dashboard."""

    userbot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="cached_messages"
    )
    chat_id = models.BigIntegerField(db_index=True)
    message_id = models.BigIntegerField()
    sender_id = models.BigIntegerField(null=True, blank=True)
    sender_name = models.CharField(max_length=128, blank=True, default="")
    sender_username = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField(blank=True, default="")
    media_type = models.CharField(max_length=24, blank=True, default="")
    outgoing = models.BooleanField(default=False)
    date = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "message_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["userbot", "chat_id", "message_id"],
                name="unique_cached_message",
            )
        ]

    def __str__(self) -> str:
        return f"msg {self.message_id} in {self.chat_id}"


class EngineCommand(models.Model):
    """
    A read request from the dashboard that the engine executes with its live
    session (process-safe RPC over the database).
    """

    class Kind(models.TextChoices):
        SYNC_DIALOGS = "sync_dialogs", "Sync chat list"
        FETCH_HISTORY = "fetch_history", "Fetch chat history"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    userbot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="commands"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    chat_id = models.BigIntegerField(null=True, blank=True)
    limit = models.IntegerField(default=50)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    result = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.kind} bot#{self.userbot_id} ({self.status})"



class GroupMessage(models.Model):
    """
    Persistent archive of EVERY message seen in a monitored group.

    Unlike `ChatMessage` (an on-demand cache that gets replaced), this is an
    append-only log written live by the engine for groups where anti-spam is
    enabled. Each row is enriched with the sender's role so the dashboard can
    show who wrote what (admin / user / scam / bot / me) instead of raw ids.
    """

    class SenderRole(models.TextChoices):
        ME = "me", "Me (this account)"
        ADMIN = "admin", "Group admin"
        BOT = "bot", "Bot"
        SCAM = "scam", "Scam / spam"
        USER = "user", "Regular user"

    group = models.ForeignKey(
        TelegramGroup, on_delete=models.CASCADE, related_name="messages"
    )
    userbot = models.ForeignKey(
        UserBot, on_delete=models.CASCADE, related_name="archived_messages"
    )
    chat_id = models.BigIntegerField(db_index=True)
    message_id = models.BigIntegerField()

    sender_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    sender_name = models.CharField(max_length=128, blank=True, default="")
    sender_username = models.CharField(max_length=64, blank=True, default="")
    sender_role = models.CharField(
        max_length=8, choices=SenderRole.choices, default=SenderRole.USER
    )
    # Telegram's own scam/fake account flags, kept for transparency.
    tg_scam_flag = models.BooleanField(default=False)

    text = models.TextField(blank=True, default="")
    media_type = models.CharField(max_length=24, blank=True, default="")
    date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-message_id"]
        indexes = [models.Index(fields=["chat_id", "message_id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["userbot", "chat_id", "message_id"],
                name="unique_archived_group_message",
            )
        ]

    def __str__(self) -> str:
        return f"[{self.sender_role}] {self.sender_name}: {self.text[:30]}"
