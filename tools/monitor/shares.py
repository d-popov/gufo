"""Shared chats: one conversation that everyone with a secret link sees and writes in, without signing in.

The owner (the admin) makes a link from the chat page, optionally starting it from a copy of the open chat, lists the links on
/shares and revokes them. A link is a random token in the URL. Whoever has it opens the shared conversation, which the monitor
keeps on the server: every message, from every visitor and from the model, is the same for all of them and appears on their
pages within a second or two. A visitor can speak to it with the speech models and have replies read aloud. They are told
that everyone with the link sees the conversation.

The limits the owner sets (an expiry, a number of messages, a number of speech requests) apply to the link as a whole. A visitor
never gets a key, the owner's other chats, projects, tools or settings: the monitor builds every request to the model itself
from the shared conversation and a whitelist, and speaks with the owner's saved voice.

This module holds the state and the rules (storage, limits, the conversation, request building); gufo-monitor.py holds the HTTP.
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

# What a visitor may send, and what the model is shown.
MESSAGE_CHARS = 8000
NAME_CHARS = 24
NOTE_CHARS = 800
CONTEXT_MESSAGES = 40         # the latest messages of the conversation the model sees
CONTEXT_CHARS = 60_000
MAX_TOKENS = 1024             # of one reply
TEXT_CHARS = 1000             # text spoken in one request
AUDIO_BYTES = 4 << 20         # one utterance as WAV
SHOWN = 200                   # the latest messages a visitor's page is sent
MAX_ROWS = 2000               # messages one link may hold
PENDING_S = 300               # a reply that has been "being written" this long is given up on

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, title TEXT NOT NULL,
  created INTEGER NOT NULL, expires INTEGER NOT NULL, max_messages INTEGER NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
  max_audio INTEGER NOT NULL, audio INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0, last_used INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0,
  chat_id TEXT NOT NULL DEFAULT '', base INTEGER NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS share_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT NOT NULL, at INTEGER NOT NULL, role TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS share_messages_share ON share_messages (share_id, id);
"""


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
            kept.append({"role": message["role"], "content": content[:MESSAGE_CHARS]})
    kept = kept[-SEED_MESSAGES:]
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    if len(json.dumps(kept, ensure_ascii=False).encode()) > SEED_BYTES:
        raise ShareError(413, "that conversation is too long to share; start a link without it")
    return kept


def clean_name(name) -> str:
    """A visitor's display name: one line of printable text, at most NAME_CHARS long; "Guest" if nothing is left."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", name if isinstance(name, str) else "").strip()
    return text[:NAME_CHARS].strip() or "Guest"


def clean_post(body) -> tuple:
    """(text, name, note) of a visitor's message. The note is a short hint from the page (the language, speak briefly) that
    carries no more weight than the visitor's own words. Raises ShareError."""
    if not isinstance(body, dict) or not isinstance(body.get("text"), str) or not body["text"].strip():
        raise ShareError(400, "expected {text: ...}")
    if len(body["text"]) > MESSAGE_CHARS:
        raise ShareError(413, f"a message is limited to {MESSAGE_CHARS} characters")
    note = body.get("note")
    return body["text"].strip(), clean_name(body.get("name")), note[:NOTE_CHARS] if isinstance(note, str) else ""


def build_request(context: list, model: str, system: str | None, note: str = "") -> dict:
    """The request sent to the model for a reply: the owner's system text and the page's note, then the shared conversation.
    Built from a whitelist: streaming always on, thinking off, the answer length capped."""
    parts = [text.strip() for text in (system, note) if isinstance(text, str) and text.strip()]
    messages = ([{"role": "system", "content": "\n\n".join(parts)}] if parts else []) + context
    return {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": MAX_TOKENS, "reasoning_effort": "none"}


class ShareStore:
    """The links and their conversations, in a SQLite file of their own. `clock` returns seconds; tests replace it."""

    def __init__(self, path: Path | str, clock=time.time):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.open("a").close()  # created with the owner's rights only
            path.chmod(0o600)
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30.0)
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # A file from an earlier version: no revision counter, and conversations kept apart by visitor.
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(shares)")}
            for name, definition in (("rev", "INTEGER NOT NULL DEFAULT 0"), ("chat_id", "TEXT NOT NULL DEFAULT ''"),
                                     ("base", "INTEGER NOT NULL DEFAULT 0"), ("seeded", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE shares ADD COLUMN {name} {definition}")
            self._adopt_old_log()

    def _adopt_old_log(self) -> None:
        """Earlier versions kept what visitors said in `share_log`, apart per visitor. Those messages are copied into the
        shared conversation of their link, oldest first, in one transaction, and only then is the old table dropped: a
        failure leaves both tables as they were."""
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'share_log'").fetchone():
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO share_messages (share_id, at, role, name, content, done) "
                "SELECT share_id, at, role, CASE role WHEN 'user' THEN 'Guest' ELSE '' END, content, 1 FROM share_log "
                "WHERE share_id IN (SELECT id FROM shares) ORDER BY at, id")
            self.conn.execute("DROP TABLE share_log")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _now(self) -> int:
        return int(self.clock())

    COLUMNS = ("id, token, title, created, expires, max_messages, messages, max_audio, audio, revoked, last_used, "
               "(SELECT COUNT(*) FROM share_messages WHERE share_id = shares.id), chat_id, base, seeded")

    def _row(self, row) -> dict:
        (share_id, token, title, created, expires, max_messages, messages, max_audio, audio, revoked, last_used,
         held, chat_id, base, seeded) = row
        now = self._now()
        status = "revoked" if revoked else "expired" if expires and expires <= now else "active"
        return {"id": share_id, "token": token, "title": title, "created": created, "expires": expires,
                "max_messages": max_messages, "messages": messages, "max_audio": max_audio, "audio": audio,
                "last_used": last_used, "held": held, "chat_id": chat_id, "base": base,
                "seeded": seeded, "status": status}

    def create(self, owner: str, title: str, days: int = DEFAULT_DAYS, max_messages: int = DEFAULT_MESSAGES,
               max_audio: int = DEFAULT_AUDIO, seed: list | None = None, chat_id: str = "", base: int = 0) -> dict:
        """A new link. days 0 means no expiry. `chat_id` is the owner's chat it shares: the conversation is that chat's,
        which `seed` (see clean_seed: text only, the latest messages) carries over into the room. `base` is how many messages
        the owner's chat had: those are never rewritten, the room's later messages are added after them. A chat has one active link at a time
        (ShareError 409). The result holds the token, which is the secret in the URL."""
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= MAX_DAYS:
            raise ShareError(400, f"days is 0 (no expiry) to {MAX_DAYS}")
        for name, value in (("max_messages", max_messages), ("max_audio", max_audio)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
                raise ShareError(400, f"{name} is 1 to {MAX_LIMIT}")
        title = (title if isinstance(title, str) else "").strip()[:TITLE_MAX] or "Shared chat"
        if chat_id and any(s["chat_id"] == chat_id and s["status"] == "active" for s in self.list(owner)):
            raise ShareError(409, "this chat is already shared; revoke its link first")
        now = self._now()
        share_id, token = secrets.token_hex(6), secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute(
                "INSERT INTO shares (id, token, owner, title, created, expires, max_messages, max_audio, chat_id, base, seeded) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (share_id, token, owner, title, now, now + days * 86400 if days else 0, max_messages, max_audio, chat_id,
                 base, len(seed or [])))
            for message in seed or []:
                self.conn.execute("INSERT INTO share_messages (share_id, at, role, name, content) VALUES (?, ?, ?, ?, ?)",
                                  (share_id, now, message["role"], "Owner" if message["role"] == "user" else "", message["content"]))
        return self.get(owner, share_id)

    def active_for_chat(self, owner: str, chat_id: str) -> dict | None:
        """The active link that makes this chat a room, with its token, or None."""
        if not chat_id:
            return None
        with self.lock:
            row = self.conn.execute(f"SELECT {self.COLUMNS}, owner, token FROM shares WHERE owner = ? AND chat_id = ? "
                                    "AND revoked = 0", (owner, chat_id)).fetchone()
        if row is None:
            return None
        share = self._row(row[:-2])
        share["owner"] = row[-2]
        return share if share["status"] == "active" else None

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
        """Revoke an active link (its conversation stays, for the owner to read); remove one already revoked or expired,
        with its conversation. Returns "revoked", "removed" or None."""
        share = self.get(owner, share_id)
        if share is None:
            return None
        with self.lock:
            if share["status"] == "active":
                self.conn.execute("UPDATE shares SET revoked = 1 WHERE id = ? AND owner = ?", (share_id, owner))
                return "revoked"
            self.conn.execute("DELETE FROM shares WHERE id = ? AND owner = ?", (share_id, owner))
            self.conn.execute("DELETE FROM share_messages WHERE share_id = ?", (share_id,))
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

    def _take(self, token: str, used: str, limit: str) -> bool:
        now = self._now()
        with self.lock:
            cursor = self.conn.execute(
                f"UPDATE shares SET {used} = {used} + 1, last_used = ? WHERE token = ? AND revoked = 0 "
                f"AND (expires = 0 OR expires > ?) AND {used} < {limit}", (now, token, now))
            return cursor.rowcount == 1

    def take_message(self, token: str) -> bool:
        """Count one message against the link; False when it is spent, revoked or expired."""
        return self._take(token, "messages", "max_messages")

    def take_audio(self, token: str) -> bool:
        """Count one speech request (listening or speaking) against the link."""
        return self._take(token, "audio", "max_audio")

    def refund_message(self, token: str) -> None:
        """Give back a message that never reached the conversation or the model."""
        with self.lock:
            self.conn.execute("UPDATE shares SET messages = MAX(messages - 1, 0) WHERE token = ?", (token,))

    # -- the conversation

    def _change(self, token: str, sql: str, args: tuple) -> sqlite3.Cursor:
        """Run one statement on the conversation and move the link's revision on, so pages polling it see the change."""
        cursor = self.conn.execute(sql, args)
        self.conn.execute("UPDATE shares SET rev = rev + 1 WHERE token = ?", (token,))
        return cursor

    def ask(self, token: str, name: str, text: str) -> int:
        """A visitor's message joins the conversation and the model's reply is opened right after it, empty and unfinished;
        returns the reply's id. Only one reply is written at a time: asking while one is, or when the link is full, raises
        ShareError (409, 403) and changes nothing."""
        now = self._now()
        with self.lock:
            self.conn.execute("UPDATE share_messages SET done = 1 WHERE role = 'assistant' AND done = 0 AND at < ?",
                              (now - PENDING_S,))
            share = self.conn.execute("SELECT id FROM shares WHERE token = ?", (token,)).fetchone()
            if share is None:
                raise ShareError(404, "not_found")
            held, writing = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(role = 'assistant' AND done = 0), 0) FROM share_messages WHERE share_id = ?",
                (share[0],)).fetchone()
            if writing:
                raise ShareError(409, "a reply is being written; wait for it to finish")
            if held + 2 > MAX_ROWS:
                raise ShareError(403, "this conversation is full")
            self._change(token, "INSERT INTO share_messages (share_id, at, role, name, content) VALUES (?, ?, 'user', ?, ?)",
                         (share[0], now, clean_name(name), text[:MESSAGE_CHARS]))
            return self._change(token, "INSERT INTO share_messages (share_id, at, role, content, done) VALUES (?, ?, 'assistant', '', 0)",
                                (share[0], now)).lastrowid

    def extend_reply(self, token: str, row_id: int, piece: str) -> None:
        with self.lock:
            self._change(token, "UPDATE share_messages SET content = content || ? WHERE id = ? AND role = 'assistant' AND done = 0",
                         (piece, row_id))

    def finish_reply(self, token: str, row_id: int) -> None:
        """The reply is complete, or as complete as it will be; one that got no text is dropped."""
        with self.lock:
            self._change(token, "UPDATE share_messages SET done = 1 WHERE id = ?", (row_id,))
            self.conn.execute("DELETE FROM share_messages WHERE id = ? AND content = ''", (row_id,))

    def conversation(self, token: str, known: int | None = None) -> dict | None:
        """What a visitor's page shows: {"rev", "messages"} (the latest SHOWN), or {"rev", "unchanged": True} when the page
        already has revision `known`."""
        with self.lock:
            share = self.conn.execute("SELECT id, rev FROM shares WHERE token = ?", (token,)).fetchone()
            if share is None:
                return None
            if known == share[1]:
                return {"rev": share[1], "unchanged": True}
            rows = self.conn.execute("SELECT id, at, role, name, content, done FROM share_messages WHERE share_id = ? "
                                     "ORDER BY id DESC LIMIT ?", (share[0], SHOWN)).fetchall()
        return {"rev": share[1], "messages": [{"id": i, "at": at, "role": role, "name": name, "content": content,
                                               "done": bool(done)} for i, at, role, name, content, done in reversed(rows)]}

    def transcript(self, owner: str, share_id: str) -> list | None:
        """The whole conversation for its owner, oldest first; None if the link is not theirs."""
        if self.get(owner, share_id) is None:
            return None
        with self.lock:
            rows = self.conn.execute("SELECT at, role, name, content FROM share_messages WHERE share_id = ? ORDER BY id",
                                     (share_id,)).fetchall()
        return [{"at": at, "role": role, "name": name, "content": content} for at, role, name, content in rows]

    def context(self, token: str) -> list:
        """The model's view of the conversation: its latest messages, finished ones only, each visitor's words tagged with
        their name when more than one person has spoken. Starts with a visitor's message and fits CONTEXT_CHARS."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT role, name, content FROM share_messages WHERE share_id = (SELECT id FROM shares WHERE token = ?) "
                "AND done = 1 AND content != '' ORDER BY id DESC LIMIT ?", (token, CONTEXT_MESSAGES)).fetchall()
        rows.reverse()
        names = {name for role, name, _ in rows if role == "user"}
        turns = [{"role": role, "content": f"{name}: {content}" if role == "user" and len(names) > 1 else content}
                 for role, name, content in rows]
        while turns and (turns[0]["role"] != "user" or sum(len(t["content"]) for t in turns) > CONTEXT_CHARS):
            turns.pop(0)
        return turns

    def view(self, share: dict, model: str | None, talk: bool, voice: dict) -> dict:
        """What a visitor's page is told about the link: nothing about the owner or the secret."""
        return {"title": share["title"], "model": model, "talk": talk, "expires": share["expires"],
                "messages_left": share["max_messages"] - share["messages"],
                "audio_left": share["max_audio"] - share["audio"], "speed": voice.get("speed", 1.0)}
