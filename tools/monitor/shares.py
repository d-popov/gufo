"""Shared chat links: a secret URL that lets someone chat, talk and listen without signing in.

The owner (the admin) creates a link from the chat page, lists the links on /shares and revokes them. A link is a
random token in the URL; whoever has it can use this server's model and speech models through the monitor, within
the limits the owner set when creating it (an expiry, a number of messages, a number of speech requests). The
visitor gets a fresh conversation kept in their own browser, optionally starting from a copy of the owner's chat.
They never get a key, the owner's other chats, projects, tools or settings: the monitor builds every request to the
model itself from a whitelist, and speaks with the owner's saved voice.

This module holds the state and the rules (storage, limits, request cleaning); gufo-monitor.py holds the HTTP.
Python 3.11+ standard library only.
"""

import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z0-9_-]{32,64}")      # the secret in a link: secrets.token_urlsafe(24) is 32 of these
SHARE_ID = re.compile(r"[0-9a-f]{12}")            # the owner's handle for a link, never the secret

DEFAULT_DAYS = 7
MAX_DAYS = 90
DEFAULT_MESSAGES = 100
DEFAULT_AUDIO = 400
MAX_LIMIT = 10_000
TITLE_MAX = 80
SEED_MESSAGES = 60
SEED_BYTES = 200_000

# What a visitor's request to the model may hold.
GUEST_MESSAGES = 60           # turns in one request
GUEST_MESSAGE_CHARS = 8000
GUEST_TOTAL_CHARS = 60_000
GUEST_NOTE_CHARS = 800
GUEST_MAX_TOKENS = 1024
GUEST_TEXT_CHARS = 1000       # text spoken in one request
GUEST_AUDIO_BYTES = 4 << 20   # one utterance as WAV

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, title TEXT NOT NULL,
  created INTEGER NOT NULL, expires INTEGER NOT NULL, max_messages INTEGER NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
  max_audio INTEGER NOT NULL, audio INTEGER NOT NULL DEFAULT 0, seed TEXT NOT NULL DEFAULT '',
  revoked INTEGER NOT NULL DEFAULT 0, last_used INTEGER NOT NULL DEFAULT 0, log INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS share_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT NOT NULL, at INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS share_log_share ON share_log (share_id, id);
"""
LOG_ROWS = 2000   # messages kept per link when its owner chose to read them; later ones are not kept


class ShareError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def clean_seed(messages) -> list:
    """The part of the owner's chat a link starts from: plain user and assistant text, nothing else (no images,
    reasoning or tool steps), limited in count and size. Raises ShareError."""
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ShareError(400, "messages is a list")
    kept = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip() and not message.get("error"):
            kept.append({"role": message["role"], "content": content[:GUEST_MESSAGE_CHARS]})
    kept = kept[-SEED_MESSAGES:]
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    if len(json.dumps(kept, ensure_ascii=False).encode()) > SEED_BYTES:
        raise ShareError(413, "that conversation is too long to share; start a link without it")
    return kept


def clean_request(body, model: str, system: str | None, note: str | None = None) -> dict:
    """The request sent to the model for a visitor, built from a whitelist. Only the visitor's own plain text is
    taken (roles user and assistant, string content); the system message is the owner's, plus a short note from the
    page (the language, a hint to speak briefly) that carries no more weight than the visitor's own words. Streaming
    is always on, thinking off, the answer length capped. Raises ShareError."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ShareError(400, "expected {messages: [...]}")
    turns = body["messages"]
    if not 1 <= len(turns) <= GUEST_MESSAGES:
        raise ShareError(400, f"between 1 and {GUEST_MESSAGES} messages")
    cleaned, total = [], 0
    for turn in turns:
        if (not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant")
                or not isinstance(turn.get("content"), str) or not turn["content"].strip()):
            raise ShareError(400, "messages hold text from the user and the assistant only")
        if len(turn["content"]) > GUEST_MESSAGE_CHARS:
            raise ShareError(413, f"a message is limited to {GUEST_MESSAGE_CHARS} characters")
        total += len(turn["content"])
        cleaned.append({"role": turn["role"], "content": turn["content"]})
    if total > GUEST_TOTAL_CHARS:
        raise ShareError(413, "the conversation is too long; start a new one")
    if cleaned[-1]["role"] != "user":
        raise ShareError(400, "the last message is the user's")
    extra = note if isinstance(note, str) else ""
    parts = [text.strip() for text in (system, extra[:GUEST_NOTE_CHARS]) if isinstance(text, str) and text.strip()]
    messages = ([{"role": "system", "content": "\n\n".join(parts)}] if parts else []) + cleaned
    return {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": GUEST_MAX_TOKENS, "reasoning_effort": "none"}


class ShareStore:
    """The links, in a SQLite file of their own. `clock` returns seconds; tests replace it."""

    def __init__(self, path: Path | str, clock=time.time):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = path.open("a")  # created with the owner's rights only
            fd.close()
            path.chmod(0o600)
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30.0)
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            if "log" not in {row[1] for row in self.conn.execute("PRAGMA table_info(shares)")}:  # a file from before logging
                self.conn.execute("ALTER TABLE shares ADD COLUMN log INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _now(self) -> int:
        return int(self.clock())

    COLUMNS = ("id, token, title, created, expires, max_messages, messages, max_audio, audio, revoked, last_used, "
               "seed != '', log")

    def _row(self, row) -> dict:
        (share_id, token, title, created, expires, max_messages, messages, max_audio, audio, revoked, last_used,
         has_seed, log) = row
        now = self._now()
        status = "revoked" if revoked else "expired" if expires and expires <= now else "active"
        return {"id": share_id, "token": token, "title": title, "created": created, "expires": expires,
                "max_messages": max_messages, "messages": messages, "max_audio": max_audio, "audio": audio,
                "last_used": last_used, "has_seed": bool(has_seed), "logged": bool(log), "status": status}

    def create(self, owner: str, title: str, days: int = DEFAULT_DAYS, max_messages: int = DEFAULT_MESSAGES,
               max_audio: int = DEFAULT_AUDIO, seed: list | None = None, log: bool = False) -> dict:
        """A new link. days 0 means no expiry. With log, the visitor's conversation is kept for the owner to read (the
        visitor's page says so). The result holds the token, which is the secret in the URL."""
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= MAX_DAYS:
            raise ShareError(400, f"days is 0 (no expiry) to {MAX_DAYS}")
        for name, value in (("max_messages", max_messages), ("max_audio", max_audio)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
                raise ShareError(400, f"{name} is 1 to {MAX_LIMIT}")
        title = (title if isinstance(title, str) else "").strip()[:TITLE_MAX] or "Shared chat"
        now = self._now()
        share_id, token = secrets.token_hex(6), secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute(
                "INSERT INTO shares (id, token, owner, title, created, expires, max_messages, max_audio, seed, log) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (share_id, token, owner, title, now, now + days * 86400 if days else 0, max_messages, max_audio,
                 json.dumps(seed, ensure_ascii=False) if seed else "", int(bool(log))))
        return self.get(owner, share_id)

    def get(self, owner: str, share_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(f"SELECT {self.COLUMNS} FROM shares WHERE id = ? AND owner = ?",
                                    (share_id, owner)).fetchone()
        return self._row(row) if row else None

    def list(self, owner: str) -> list:
        with self.lock:
            rows = self.conn.execute(f"SELECT {self.COLUMNS} FROM shares WHERE owner = ? ORDER BY created DESC",
                                     (owner,)).fetchall()
        return [self._row(row) for row in rows]

    def remove(self, owner: str, share_id: str) -> str | None:
        """Revoke an active link; remove one already revoked or expired. Returns "revoked", "removed" or None."""
        share = self.get(owner, share_id)
        if share is None:
            return None
        with self.lock:
            if share["status"] == "active":
                self.conn.execute("UPDATE shares SET revoked = 1 WHERE id = ? AND owner = ?", (share_id, owner))
                return "revoked"
            self.conn.execute("DELETE FROM shares WHERE id = ? AND owner = ?", (share_id, owner))
            self.conn.execute("DELETE FROM share_log WHERE share_id = ?", (share_id,))
            return "removed"

    def find(self, token: str) -> dict | None:
        """The link a visitor holds, if it is usable now: not revoked, not expired. Anything else is None, and the
        three reasons are not told apart."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        with self.lock:
            row = self.conn.execute(f"SELECT {self.COLUMNS}, owner FROM shares WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        share = self._row(row[:-1])
        share["owner"] = row[-1]
        return share if share["status"] == "active" else None

    def seed(self, token: str) -> list:
        with self.lock:
            row = self.conn.execute("SELECT seed FROM shares WHERE token = ?", (token,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else []
        except ValueError:
            return []

    def _take(self, token: str, used: str, limit: str) -> bool:
        now = self._now()
        with self.lock:
            cursor = self.conn.execute(
                f"UPDATE shares SET {used} = {used} + 1, last_used = ? WHERE token = ? AND revoked = 0 "
                f"AND (expires = 0 OR expires > ?) AND {used} < {limit}", (now, token, now))
            return cursor.rowcount == 1

    def take_message(self, token: str) -> bool:
        """Count one chat message against the link; False when it is spent, revoked or expired."""
        return self._take(token, "messages", "max_messages")

    def take_audio(self, token: str) -> bool:
        """Count one speech request (listening or speaking) against the link."""
        return self._take(token, "audio", "max_audio")

    def refund_message(self, token: str) -> None:
        """Give back a message the model never got to see (it refused the request)."""
        with self.lock:
            self.conn.execute("UPDATE shares SET messages = MAX(messages - 1, 0) WHERE token = ?", (token,))

    def record(self, token: str, role: str, content: str) -> None:
        """Keep one message of a visitor's conversation, if the link's owner chose to read them, up to LOG_ROWS."""
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            return
        with self.lock:
            self.conn.execute(
                "INSERT INTO share_log (share_id, at, role, content) SELECT id, ?, ?, ? FROM shares WHERE token = ? "
                "AND log = 1 AND (SELECT COUNT(*) FROM share_log WHERE share_id = shares.id) < ?",
                (self._now(), role, content[:GUEST_MESSAGE_CHARS], token, LOG_ROWS))

    def messages(self, owner: str, share_id: str) -> list | None:
        """What a link's visitors said and were answered, oldest first; None if the link is not this owner's."""
        if self.get(owner, share_id) is None:
            return None
        with self.lock:
            rows = self.conn.execute("SELECT at, role, content FROM share_log WHERE share_id = ? ORDER BY id",
                                     (share_id,)).fetchall()
        return [{"at": at, "role": role, "content": content} for at, role, content in rows]

    def view(self, share: dict, model: str | None, talk: bool, voice: dict) -> dict:
        """What a visitor's page is told: nothing about the owner or the secret."""
        return {"title": share["title"], "model": model, "talk": talk, "expires": share["expires"],
                "messages_left": share["max_messages"] - share["messages"],
                "audio_left": share["max_audio"] - share["audio"], "seed": self.seed(share["token"]), "logged": share["logged"],
                "speed": voice.get("speed", 1.0)}
