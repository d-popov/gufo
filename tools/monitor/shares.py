"""Shared chats: one conversation that everyone with a secret link sees and writes in, without signing in.

The owner (the admin) makes a link from the chat page, optionally starting it from a copy of the open chat, lists the links on
/shares and revokes them. A link is a random token in the URL. Whoever has it opens the shared conversation, which the monitor
keeps on the server: every message, from every visitor and from the model, is the same for all of them and appears on their
pages within a second or two, each reply with its reasoning, its tool steps and its stats. A visitor can speak to it with the
speech models and have replies read aloud. They are told that everyone with the link sees the conversation.

The limits the owner sets (an expiry, a number of messages, a number of speech requests) apply to the link as a whole, and so do
the owner's choices of browser tools and reasoning effort, which visitors see but cannot change (a visitor may pick the effort of
their own messages). A visitor never gets a key, the owner's other chats, projects or settings: the monitor builds every
request to the model itself from the shared conversation and a whitelist. The tools run in the browser of the visitor who asked,
like the owner's chat runs them in the owner's: the monitor offers the model only the owner's tools, takes back only well-formed
calls of them and their results, and keeps the reply, its steps and its stats itself. When the owner enables the library's two
read-only tools for a link, its visitors can search and read the whole library through the link's own endpoint, and nothing
else of it.

This module holds the state and the rules (storage, limits, the conversation, request building); gufo-monitor.py holds the HTTP.
Python 3.11+ standard library only.
"""

import base64
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
SEED_BYTES = 4 << 20          # the text of a chat that can be shared: a chat is stored up to this size

# The tools a room may offer, in the order of static/tools.js. The library's two read-only tools, when the owner enables them,
# let everyone with the link search and read the whole library (gufo-monitor.py answers them for the link).
ROOM_TOOLS = ("web_search", "fetch_url", "find_media", "search_library", "read_library", "run_python")
LIBRARY_TOOLS = ("search_library", "read_library")
EFFORTS = ("", "none", "low", "medium", "high", "max")   # "": the owner's choice, or the model's default
MAX_ROUNDS = 6                # model calls of one reply; the last one is offered no tools

# What a visitor may send, and what the model is shown.
MESSAGE_CHARS = 8000
NAME_CHARS = 24
NOTE_CHARS = 800
TOOL_NOTE_CHARS = 4000        # the page's description of its tools, for the system text
TOOLS_CHARS = 20_000          # the tool schemas the page offers, as JSON
EXTRA_MESSAGES = 40           # tool calls and results of one reply
ARGUMENTS_CHARS = 8000        # of one tool call
TOOL_CONTENT_CHARS = 30_000   # of one tool result
EXTRA_CHARS = 300_000         # of all of them, as JSON
CHAT_BYTES = 2 << 20          # a request body that carries them
CONTEXT_MESSAGES = 40         # the latest messages of the conversation the model sees
CONTEXT_CHARS = 60_000
MAX_TOKENS = 8192             # of one model round; thinking needs room
TEXT_CHARS = 1000             # text spoken in one request
AUDIO_BYTES = 4 << 20         # one utterance as WAV
SHOWN = 200                   # the latest messages a visitor's page is sent
MAX_ROWS = 2000               # messages one link may hold
PENDING_S = 300               # a reply nothing has been written to for this long is given up on

# A reply's tool steps as the asker's page reports them.
STEPS_MAX = 12
STEP_STATES = ("run", "ok", "bad")
LABEL_CHARS = 200
CODE_CHARS = 4000
EXCERPT_CHARS = 1500
MEDIA_MAX = 8                 # found media of one step (static/webtools.js MEDIA_MAX)
FIGURES_PER_STEP = 4
FIGURE_BYTES = 3 << 19        # 1.5 MiB, one drawn figure as PNG
FIGURES_BYTES = 6 << 20       # all the figures of one reply
STEPS_BYTES = 9 << 20         # a request body that carries them, base64
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_DATA_URL = "data:image/png;base64,"
MEDIA_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")
MEDIA_URL = re.compile(r"https://([a-z0-9.-]+)(?:[/?#][^\s\\]*)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, title TEXT NOT NULL,
  created INTEGER NOT NULL, expires INTEGER NOT NULL, max_messages INTEGER NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
  max_audio INTEGER NOT NULL, audio INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0, last_used INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0,
  chat_id TEXT NOT NULL DEFAULT '', base INTEGER NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0,
  tools TEXT NOT NULL DEFAULT '[]', effort TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS share_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT NOT NULL, at INTEGER NOT NULL, role TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 1,
  meta TEXT NOT NULL DEFAULT '', touched INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS share_messages_share ON share_messages (share_id, id);
CREATE TABLE IF NOT EXISTS share_figures (
  message_id INTEGER NOT NULL, step INTEGER NOT NULL, idx INTEGER NOT NULL, png BLOB NOT NULL,
  PRIMARY KEY (message_id, step, idx));
"""


class ShareError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


REASONING_CHARS = 100_000     # of one seeded reply's thinking
STATS_KEYS = ("tps", "ttft_ms", "prompt_tokens", "cached_tokens", "completion_tokens", "calls")


def seed_meta(message: dict) -> dict:
    """What the owner's chat holds of a reply besides its text, as a room shows it: the thinking, the tool steps (their
    figures only as a count: the owner's chat keeps no figure bytes, see static/chat.js) and the stats without the cost."""
    meta = {}
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        meta["reasoning"] = reasoning[:REASONING_CHARS]
    if isinstance(message.get("steps"), list):
        steps, _ = clean_steps(message["steps"], ROOM_TOOLS)
        raw = [step for step in message["steps"] if isinstance(step, dict) and step.get("name") in ROOM_TOOLS
               and step.get("state") in STEP_STATES][:len(steps)]
        for step, source in zip(steps, raw):
            count = source.get("figureCount")
            if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= 99:
                step["figureCount"] = count
            elif isinstance(source.get("figures"), list) and source["figures"]:
                step["figureCount"] = min(len(source["figures"]), 99)
        if steps:
            meta["steps"] = steps
    stats = message.get("stats")
    if isinstance(stats, dict):
        kept = {key: stats[key] for key in STATS_KEYS
                if isinstance(stats.get(key), (int, float)) and not isinstance(stats[key], bool)}
        if kept:
            meta["stats"] = kept
    return meta


def clean_seed(messages) -> list:
    """The conversation of the owner's chat that a room starts with: every user and assistant message, whole, in order (no
    images); a reply keeps its thinking, tool steps and stats as `meta` (see seed_meta). Visitors see all of it; only what
    the model reads is capped (see context). Raises ShareError when it cannot be held: more than MAX_ROWS messages, or more
    than SEED_BYTES of text."""
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
            row = {"role": message["role"], "content": content}
            meta = seed_meta(message) if message["role"] == "assistant" else {}
            if meta:
                row["meta"] = meta
            kept.append(row)
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    if len(kept) > MAX_ROWS // 2 or len(json.dumps(kept, ensure_ascii=False).encode()) > SEED_BYTES:
        raise ShareError(413, "that chat is too long to share as a room")
    return kept


def clean_name(name) -> str:
    """A visitor's display name: one line of printable text, at most NAME_CHARS long; "Guest" if nothing is left."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", name if isinstance(name, str) else "").strip()
    return text[:NAME_CHARS].strip() or "Guest"


def clean_note(note, limit: int = NOTE_CHARS) -> str:
    """A hint from the page for the system text, cut to `limit`; "" if it is not text."""
    return note[:limit] if isinstance(note, str) else ""


def clean_post(body) -> tuple:
    """(text, name, note) of a visitor's message. The note is a short hint from the page (the language, speak briefly) that
    carries no more weight than the visitor's own words. Raises ShareError."""
    if not isinstance(body, dict) or not isinstance(body.get("text"), str) or not body["text"].strip():
        raise ShareError(400, "expected {text: ...}")
    if len(body["text"]) > MESSAGE_CHARS:
        raise ShareError(413, f"a message is limited to {MESSAGE_CHARS} characters")
    return body["text"].strip(), clean_name(body.get("name")), clean_note(body.get("note"))


def clean_tools(ids) -> list:
    """The owner's choice of room tools: the ROOM_TOOLS named in `ids`, in their order; anything else (unknown names) is
    dropped. Raises ShareError unless it is a list (or absent: none)."""
    if ids is None:
        return []
    if not isinstance(ids, list):
        raise ShareError(400, "tools is a list of tool names")
    return [name for name in ROOM_TOOLS if name in ids]


def clean_effort(effort) -> str:
    """A reasoning effort, one of EFFORTS; absent is "". Raises ShareError."""
    if effort is None:
        return ""
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ShareError(400, "effort is one of " + ", ".join(repr(e) for e in EFFORTS))
    return effort


def room_tools(share: dict, python: bool, library: bool) -> list:
    """The tools a room offers now: the owner's choice, without Python when its runtime is not installed and without the
    library's tools when the monitor has no library."""
    return [name for name in share["tools"]
            if (python or name != "run_python") and (library or name not in LIBRARY_TOOLS)]


def clean_offered(tools, enabled) -> list:
    """The tool schemas the asker's page offers the model, kept only for the room's `enabled` tools and rebuilt from a
    whitelist (name, description, parameters). Raises ShareError if they are not a list or too long."""
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ShareError(400, "tools is a list of tool schemas")
    kept, names = [], set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) and tool.get("type") == "function" else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or name not in enabled or name in names:
            continue
        names.add(name)
        description, parameters = function.get("description"), function.get("parameters")
        kept.append({"type": "function", "function": {
            "name": name, "description": description if isinstance(description, str) else "",
            "parameters": parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}}})
    if len(json.dumps(kept, ensure_ascii=False)) > TOOLS_CHARS:
        raise ShareError(413, f"the tool schemas are limited to {TOOLS_CHARS} characters")
    return kept


def clean_extra(extra, enabled) -> list:
    """What the model and the asker's browser added to a reply since its question, for the next model round: assistant
    messages with calls of `enabled` tools, each followed by a result of every call. Rebuilt from a whitelist; anything else
    raises ShareError (400, 413 for sizes)."""
    def bad():
        return ShareError(400, "extra is the reply's tool calls, each followed by its results")
    if not isinstance(extra, list) or not 2 <= len(extra) <= EXTRA_MESSAGES:
        raise bad()
    kept, open_calls, seen = [], set(), set()
    for message in extra:
        if not isinstance(message, dict):
            raise bad()
        if message.get("role") == "assistant":
            content, tool_calls = message.get("content"), message.get("tool_calls")
            if open_calls or (content is not None and not isinstance(content, str)) or not isinstance(tool_calls, list) \
                    or not tool_calls:
                raise bad()
            calls = []
            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                call_id = call.get("id") if isinstance(call, dict) else None
                if not (isinstance(call_id, str) and 0 < len(call_id) <= 100 and call_id not in seen
                        and call.get("type") == "function" and isinstance(function, dict)
                        and isinstance(function.get("name"), str) and function["name"] in enabled
                        and isinstance(function.get("arguments"), str)):
                    raise bad()
                if len(function["arguments"]) > ARGUMENTS_CHARS:
                    raise ShareError(413, f"a tool call's arguments are limited to {ARGUMENTS_CHARS} characters")
                seen.add(call_id)
                open_calls.add(call_id)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": function["name"], "arguments": function["arguments"]}})
            kept.append({"role": "assistant", "content": content, "tool_calls": calls})
        elif message.get("role") == "tool":
            call_id, content = message.get("tool_call_id"), message.get("content")
            if not isinstance(call_id, str) or call_id not in open_calls or not isinstance(content, str):
                raise bad()
            if len(content) > TOOL_CONTENT_CHARS:
                raise ShareError(413, f"a tool result is limited to {TOOL_CONTENT_CHARS} characters")
            open_calls.discard(call_id)
            kept.append({"role": "tool", "tool_call_id": call_id, "content": content})
        else:
            raise bad()
    if open_calls or kept[0]["role"] != "assistant":
        raise bad()
    if len(json.dumps(kept, ensure_ascii=False)) > EXTRA_CHARS:
        raise ShareError(413, f"a reply's tool calls and results are limited to {EXTRA_CHARS} characters")
    return kept


def _media_url(value, hosts) -> str | None:
    """An https address on one of `hosts`, with no user, port or anything a browser could read as another host."""
    match = MEDIA_URL.fullmatch(value) if isinstance(value, str) else None
    return value if match and match.group(1) in hosts else None


def _plain(value, limit: int) -> str:
    text = re.sub(r"<[^>]*>", "", "" if value is None else str(value))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def safe_media(item) -> dict | None:
    """One found picture or video as a room may show it, or None: static/webtools.js safeMedia, on the server."""
    if not isinstance(item, dict):
        return None
    kind = item.get("kind") if item.get("kind") in ("image", "video") else None
    thumb = _media_url(item.get("thumb"), MEDIA_HOSTS)
    page = _media_url(item.get("page"), ("commons.wikimedia.org",))
    src = _media_url(item.get("src"), ("upload.wikimedia.org",)) if kind == "video" else thumb
    if not kind or not thumb or not page or not src:
        return None
    return {"kind": kind, "title": _plain(item.get("title"), 120), "src": src, "thumb": thumb, "page": page,
            "license": _plain(item.get("license"), 60), "author": _plain(item.get("author"), 80)}


def figure_png(url) -> bytes | None:
    """The PNG of a drawn figure sent as a data: URL, or None if it is not one or is too big."""
    if not isinstance(url, str) or not url.startswith(PNG_DATA_URL) or len(url) > len(PNG_DATA_URL) + FIGURE_BYTES * 4 // 3 + 4:
        return None
    try:
        png = base64.b64decode(url[len(PNG_DATA_URL):], validate=True)
    except ValueError:
        return None
    return png if png.startswith(PNG_SIGNATURE) and len(png) <= FIGURE_BYTES else None


def clean_steps(steps, enabled) -> tuple:
    """(steps, figures) of a reply as the asker's page reports them. Steps of tools the room does not offer, or in no known
    state, are dropped and the rest clipped: at most STEPS_MAX, their texts cut, only safe media, at most FIGURES_PER_STEP
    PNG figures each and FIGURES_BYTES in all, and neither for the library's tools. A kept step holds its figures' count
    under "figures"; `figures` is [(step, index, png)]. Raises ShareError unless `steps` is a list."""
    if not isinstance(steps, list):
        raise ShareError(400, "steps is a list")
    kept, figures, total = [], [], 0
    for step in steps:
        if len(kept) == STEPS_MAX:
            break
        if not isinstance(step, dict) or not isinstance(step.get("name"), str) or step["name"] not in enabled \
                or step.get("state") not in STEP_STATES:
            continue
        label, code, excerpt = step.get("label"), step.get("code"), step.get("excerpt")
        clean = {"name": step["name"], "label": label[:LABEL_CHARS] if isinstance(label, str) else "", "state": step["state"]}
        if step.get("via") == "reader":
            clean["via"] = "reader"
        if isinstance(code, str) and code:
            clean["code"] = code[:CODE_CHARS]
        clean["excerpt"] = excerpt[:EXCERPT_CHARS] if isinstance(excerpt, str) else ""
        shows = step["name"] not in LIBRARY_TOOLS
        media = [found for found in map(safe_media, step["media"][:MEDIA_MAX])
                 if found] if shows and isinstance(step.get("media"), list) else []
        if media:
            clean["media"] = media
        count = 0
        for url in step["figures"][:FIGURES_PER_STEP] if shows and isinstance(step.get("figures"), list) else []:
            png = figure_png(url)
            if png is None or total + len(png) > FIGURES_BYTES:
                continue
            total += len(png)
            figures.append((len(kept), count, png))
            count += 1
        if count:
            clean["figures"] = count
        kept.append(clean)
    return kept, figures


def build_request(context: list, model: str, system: str | None, note: str = "", tool_note: str = "", effort: str = "",
                  tools: list | None = None) -> dict:
    """The request sent to the model for one round of a reply: the owner's system text, the page's note and its tools' note,
    then the shared conversation (and the reply's tool calls so far). Built from a whitelist: streaming always on, the
    answer length capped, the effort only when someone chose one, the tools only when offered."""
    parts = [text.strip() for text in (system, note, tool_note) if isinstance(text, str) and text.strip()]
    messages = ([{"role": "system", "content": "\n\n".join(parts)}] if parts else []) + context
    request = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
               "max_tokens": MAX_TOKENS}
    if effort:
        request["reasoning_effort"] = effort
    if tools:
        request["tools"] = tools
    return request


def _count(value) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


class Turn:
    """A reply being written, which may take several model rounds with the asker's tools run between them: the secret the
    asker continues it with, its rounds so far, whether one is streaming, and the sums its stats are made of (as
    static/chat.js sums them). Lives in memory: a restart gives the reply up."""

    def __init__(self, token: str, clock=time.monotonic):
        self.token, self.clock = token, clock
        self.nonce = secrets.token_urlsafe(18)
        self.rounds = 0             # the index of the current round
        self.streaming = True
        self.aborted = False
        self.started = clock()      # the first round's request
        self.first = None           # the reply's first piece of reasoning or text
        self.pieces = 0
        self.prompt = self.cached = self.completion = 0
        self.decode = 0.0           # seconds of decoding, from the server's rates

    def piece(self) -> None:
        if self.first is None:
            self.first = self.clock()
        self.pieces += 1

    def add_usage(self, usage, timings) -> None:
        """One round's usage chunk (and llama.cpp-style timings)."""
        if not isinstance(usage, dict):
            return
        completion = _count(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) and "cached_tokens" in details \
            else usage.get("cached_tokens")
        self.prompt += _count(usage.get("prompt_tokens"))
        self.cached += _count(cached)
        self.completion += completion
        rate = _count(usage.get("completion_tokens_per_second")) or (
            _count(timings.get("predicted_per_second")) if isinstance(timings, dict) else 0)
        if rate and completion:
            self.decode += completion / rate

    def stats(self, calls: int) -> dict:
        """The reply's stats line, or {} when the model reported no usage."""
        if not (self.prompt or self.completion):
            return {}
        elapsed = self.clock() - self.first if self.first is not None else 0
        tps = (self.completion / self.decode if self.decode > 0
               else self.pieces / elapsed if elapsed > 0 and self.pieces > 1 else None)
        stats = {}
        if tps:
            stats["tps"] = round(tps, 2)
        if self.first is not None:
            stats["ttft_ms"] = round((self.first - self.started) * 1000)
        stats.update(prompt_tokens=self.prompt, cached_tokens=self.cached, completion_tokens=self.completion)
        if calls:
            stats["calls"] = calls
        return stats


def _meta(text: str) -> dict:
    try:
        meta = json.loads(text) if text else {}
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


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
        self.turns: dict[int, Turn] = {}  # the replies being written, by message id
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30.0)
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # A file from an earlier version: columns added since, and conversations kept apart by visitor.
            for table, added in (("shares", (("rev", "INTEGER NOT NULL DEFAULT 0"), ("chat_id", "TEXT NOT NULL DEFAULT ''"),
                                             ("base", "INTEGER NOT NULL DEFAULT 0"), ("seeded", "INTEGER NOT NULL DEFAULT 0"),
                                             ("tools", "TEXT NOT NULL DEFAULT '[]'"), ("effort", "TEXT NOT NULL DEFAULT ''"))),
                                 ("share_messages", (("meta", "TEXT NOT NULL DEFAULT ''"),
                                                     ("touched", "INTEGER NOT NULL DEFAULT 0")))):
                columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in added:
                    if name not in columns:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
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
               "(SELECT COUNT(*) FROM share_messages WHERE share_id = shares.id), chat_id, base, seeded, tools, effort")

    def _row(self, row) -> dict:
        (share_id, token, title, created, expires, max_messages, messages, max_audio, audio, revoked, last_used,
         held, chat_id, base, seeded, tools, effort) = row
        now = self._now()
        status = "revoked" if revoked else "expired" if expires and expires <= now else "active"
        return {"id": share_id, "token": token, "title": title, "created": created, "expires": expires,
                "max_messages": max_messages, "messages": messages, "max_audio": max_audio, "audio": audio,
                "last_used": last_used, "held": held, "chat_id": chat_id, "base": base,
                "seeded": seeded, "status": status, "tools": clean_tools(json.loads(tools)), "effort": effort}

    def create(self, owner: str, title: str, days: int = DEFAULT_DAYS, max_messages: int = DEFAULT_MESSAGES,
               max_audio: int = DEFAULT_AUDIO, seed: list | None = None, chat_id: str = "", base: int = 0,
               tools=None, effort=None) -> dict:
        """A new link. days 0 means no expiry. `chat_id` is the owner's chat it shares: the conversation is that chat's,
        which `seed` (see clean_seed: text only, the latest messages) carries over into the room. `base` is how many messages
        the owner's chat had: those are never rewritten, the room's later messages are added after them. `tools` and
        `effort` are the owner's chat settings the room uses (see clean_tools, clean_effort). A chat has one active link at
        a time (ShareError 409). The result holds the token, which is the secret in the URL."""
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= MAX_DAYS:
            raise ShareError(400, f"days is 0 (no expiry) to {MAX_DAYS}")
        for name, value in (("max_messages", max_messages), ("max_audio", max_audio)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
                raise ShareError(400, f"{name} is 1 to {MAX_LIMIT}")
        tools, effort = clean_tools(tools), clean_effort(effort)
        title = (title if isinstance(title, str) else "").strip()[:TITLE_MAX] or "Shared chat"
        if chat_id and any(s["chat_id"] == chat_id and s["status"] == "active" for s in self.list(owner)):
            raise ShareError(409, "this chat is already shared; revoke its link first")
        now = self._now()
        share_id, token = secrets.token_hex(6), secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute(
                "INSERT INTO shares (id, token, owner, title, created, expires, max_messages, max_audio, chat_id, base, seeded, "
                "tools, effort) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (share_id, token, owner, title, now, now + days * 86400 if days else 0, max_messages, max_audio, chat_id,
                 base, len(seed or []), json.dumps(tools), effort))
            for message in seed or []:
                self.conn.execute("INSERT INTO share_messages (share_id, at, role, name, content, meta) VALUES (?, ?, ?, ?, ?, ?)",
                                  (share_id, now, message["role"], "Owner" if message["role"] == "user" else "",
                                   message["content"], json.dumps(message["meta"], ensure_ascii=False) if message.get("meta") else ""))
        return self.get(owner, share_id)

    def refresh_seed(self, owner: str, share_id: str, messages) -> None:
        """A room made before its replies carried their thinking and steps: the seeded replies that hold none take them, and
        the full text if they were seeded part-written, from the owner's chat `messages` (its first `base`), matched in order
        by role and by the text they start with. Nothing else is touched."""
        share = self.get(owner, share_id)
        if share is None or share["status"] != "active" or not share["seeded"] or not isinstance(messages, list):
            return
        try:
            seed = clean_seed(messages[:share["base"]])
        except ShareError:
            return
        with self.lock:
            rows = self.conn.execute("SELECT id, role, content, meta FROM share_messages WHERE share_id = ? ORDER BY id LIMIT ?",
                                     (share_id, share["seeded"])).fetchall()
            changed = False
            for (row_id, role, content, meta), message in zip(rows, seed):
                # A reply that was still being written when the link was made was seeded as far as it had got.
                if role == message["role"] and message["content"].startswith(content) and not meta and message.get("meta"):
                    self.conn.execute("UPDATE share_messages SET content = ?, meta = ? WHERE id = ?",
                                      (message["content"], json.dumps(message["meta"], ensure_ascii=False), row_id))
                    changed = True
            if changed:
                self.conn.execute("UPDATE shares SET rev = rev + 1 WHERE id = ?", (share_id,))

    def settings(self, owner: str, share_id: str, tools, effort) -> dict | None:
        """The owner changed the tools or the effort of the chat an active link shares: the room follows. None if the link
        is not theirs or no longer active; ShareError for a bad value."""
        tools, effort = clean_tools(tools), clean_effort(effort)
        share = self.get(owner, share_id)
        if share is None or share["status"] != "active":
            return None
        with self.lock:
            self.conn.execute("UPDATE shares SET tools = ?, effort = ? WHERE id = ? AND owner = ?",
                              (json.dumps(tools), effort, share_id, owner))
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
            self.conn.execute("DELETE FROM share_figures WHERE message_id IN "
                              "(SELECT id FROM share_messages WHERE share_id = ?)", (share_id,))
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

    def _give_up(self, now: int) -> None:
        """Replies nothing was written to for PENDING_S are finished as they are: their asker left mid-way."""
        stale = [row for (row,) in self.conn.execute(
            "SELECT id FROM share_messages WHERE role = 'assistant' AND done = 0 AND touched < ?", (now - PENDING_S,))]
        for row in stale:
            self.turns.pop(row, None)
            self.conn.execute("UPDATE shares SET rev = rev + 1 WHERE id = (SELECT share_id FROM share_messages WHERE id = ?)",
                              (row,))
            self.conn.execute("UPDATE share_messages SET done = 1 WHERE id = ?", (row,))

    def ask(self, token: str, name: str, text: str) -> int:
        """A visitor's message joins the conversation and the model's reply is opened right after it, empty and unfinished,
        with its Turn (see turn); returns the reply's id. Only one reply is written at a time: asking while one is, or when
        the link is full, raises ShareError (409, 403) and changes nothing."""
        now = self._now()
        with self.lock:
            self._give_up(now)
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
            row = self._change(token, "INSERT INTO share_messages (share_id, at, role, content, done, touched) "
                                      "VALUES (?, ?, 'assistant', '', 0, ?)", (share[0], now, now)).lastrowid
            self.turns[row] = Turn(token)
            return row

    def turn(self, row_id: int) -> Turn | None:
        """The Turn of a reply being written: its secret goes to the asker alone."""
        with self.lock:
            return self.turns.get(row_id)

    def _open_turn(self, token: str, row_id: int, nonce) -> Turn:
        """The Turn of an open reply of this link that `nonce` continues; ShareError 403 for anyone else, 409 when the
        reply is no longer being written."""
        turn = self.turns.get(row_id)
        if turn is None or turn.token != token or not isinstance(nonce, str) \
                or not secrets.compare_digest(turn.nonce.encode(), nonce.encode()):
            raise ShareError(403, "this reply is not yours to continue")
        if not self.conn.execute("SELECT 1 FROM share_messages WHERE id = ? AND role = 'assistant' AND done = 0",
                                 (row_id,)).fetchone():
            self.turns.pop(row_id, None)
            raise ShareError(409, "this reply is finished")
        return turn

    def begin_round(self, token: str, row_id: int, nonce) -> Turn:
        """The next model round of a reply, once its asker's tools have run: returns its Turn, whose `rounds` is the round's
        index. One round of a reply streams at a time; after MAX_ROUNDS there are none. Raises ShareError."""
        with self.lock:
            self._give_up(self._now())
            turn = self._open_turn(token, row_id, nonce)
            if turn.streaming:
                raise ShareError(409, "a round of this reply is being written")
            if turn.rounds + 1 >= MAX_ROUNDS:
                raise ShareError(409, "this reply has had all its rounds")
            turn.rounds += 1
            turn.streaming = True
            return turn

    def extend_reply(self, token: str, row_id: int, piece: str, reasoning: str = "", separate: bool = False) -> None:
        """More of a reply being written: its text and its reasoning. `separate` puts a blank line before the text when the
        reply already has some (the first text of a later round)."""
        now = self._now()
        with self.lock:
            found = self.conn.execute("SELECT content != '', meta FROM share_messages WHERE id = ? AND role = 'assistant' "
                                      "AND done = 0", (row_id,)).fetchone()
            if found is None:
                return
            meta = _meta(found[1])
            if reasoning:
                meta["reasoning"] = meta.get("reasoning", "") + reasoning
            prefix = "\n\n" if separate and found[0] and piece else ""
            self._change(token, "UPDATE share_messages SET content = content || ?, meta = ?, touched = ? WHERE id = ?",
                         (prefix + piece, json.dumps(meta, ensure_ascii=False) if meta else "", now, row_id))

    def set_steps(self, token: str, row_id: int, nonce, steps: list | None, figures: list = (), abort: bool = False) -> None:
        """The asker's page reports the reply's tool steps (see clean_steps): they replace the earlier ones, figures
        included. With `abort` the reply is finished now (the asker stopped, or their page failed). Raises ShareError."""
        now = self._now()
        with self.lock:
            turn = self._open_turn(token, row_id, nonce)
            if steps is not None:
                found = self.conn.execute("SELECT meta FROM share_messages WHERE id = ?", (row_id,)).fetchone()
                meta = _meta(found[0])
                meta.pop("steps", None)
                if steps:
                    meta["steps"] = steps
                self.conn.execute("DELETE FROM share_figures WHERE message_id = ?", (row_id,))
                self.conn.executemany("INSERT INTO share_figures (message_id, step, idx, png) VALUES (?, ?, ?, ?)",
                                      [(row_id, step, index, png) for step, index, png in figures])
                self._change(token, "UPDATE share_messages SET meta = ?, touched = ? WHERE id = ?",
                             (json.dumps(meta, ensure_ascii=False) if meta else "", now, row_id))
            if abort:
                turn.aborted = True
                self._finish(token, row_id)

    def end_round(self, token: str, row_id: int, keep_open: bool) -> None:
        """A model round of a reply is over. With `keep_open` (the model called tools, which the asker's browser now runs)
        the reply waits for its next round; otherwise it is finished."""
        with self.lock:
            turn = self.turns.get(row_id)
            if keep_open and turn is not None and not turn.aborted:
                turn.streaming = False
                self.conn.execute("UPDATE share_messages SET touched = ? WHERE id = ?", (self._now(), row_id))
            else:
                self._finish(token, row_id)

    def _finish(self, token: str, row_id: int) -> None:
        """Finish a reply with its stats; one that got no text and no steps is dropped."""
        turn = self.turns.pop(row_id, None)
        found = self.conn.execute("SELECT meta FROM share_messages WHERE id = ? AND done = 0", (row_id,)).fetchone()
        if found is None:
            return
        meta = _meta(found[0])
        stats = turn.stats(len(meta.get("steps", []))) if turn is not None else {}
        if stats:
            meta["stats"] = stats
        self._change(token, "UPDATE share_messages SET done = 1, meta = ?, touched = ? WHERE id = ?",
                     (json.dumps(meta, ensure_ascii=False) if meta else "", self._now(), row_id))
        if not meta.get("steps"):
            self.conn.execute("DELETE FROM share_messages WHERE id = ? AND content = ''", (row_id,))

    @staticmethod
    def _extras(meta_text: str, figure_url) -> dict:
        """A message's reasoning, steps and stats, those it has. `figure_url(step, n)` names a figure, or None for a count."""
        meta, extras = _meta(meta_text), {}
        if meta.get("reasoning"):
            extras["reasoning"] = meta["reasoning"]
        if meta.get("steps"):
            extras["steps"] = []
            for index, step in enumerate(meta["steps"]):
                shown = {k: v for k, v in step.items() if k != "figures"}
                if step.get("figures"):
                    if figure_url is None:
                        shown["figureCount"] = step["figures"]
                    else:
                        shown["figures"] = [figure_url(index, n) for n in range(step["figures"])]
                extras["steps"].append(shown)
        if meta.get("stats"):
            extras["stats"] = meta["stats"]
        return extras

    def conversation(self, token: str, known: int | None = None, before: int | None = None) -> dict | None:
        """What a visitor's page shows: {"rev", "messages", "more"}, the latest SHOWN messages and whether there are earlier
        ones, or {"rev", "unchanged": True} when the page already has revision `known`. With `before` (a message id) it
        is the SHOWN messages before that one instead, for a page that scrolls back through the history. A reply carries
        its reasoning, steps (figures as URLs under the link) and stats when it has them."""
        with self.lock:
            share = self.conn.execute("SELECT id, rev FROM shares WHERE token = ?", (token,)).fetchone()
            if share is None:
                return None
            if before is None and known == share[1]:
                return {"rev": share[1], "unchanged": True}
            rows = self.conn.execute(
                "SELECT id, at, role, name, content, done, meta FROM share_messages WHERE share_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT ?", (share[0], before if before is not None else 1 << 62, SHOWN + 1)).fetchall()
        more = len(rows) > SHOWN
        return {"rev": share[1], "more": more,
                "messages": [{"id": i, "at": at, "role": role, "name": name, "content": content, "done": bool(done),
                              **self._extras(meta, lambda step, n, i=i: f"/api/share/{token}/figure/{i}/{step}/{n}.png")}
                             for i, at, role, name, content, done, meta in reversed(rows[:SHOWN])]}

    def figure(self, token: str, row_id: int, step: int, index: int) -> bytes | None:
        """A drawn figure of a reply in this link's conversation, as PNG."""
        with self.lock:
            found = self.conn.execute(
                "SELECT png FROM share_figures WHERE message_id = ? AND step = ? AND idx = ? AND message_id IN "
                "(SELECT id FROM share_messages WHERE share_id = (SELECT id FROM shares WHERE token = ?))",
                (row_id, step, index, token)).fetchone()
        return found[0] if found else None

    def transcript(self, owner: str, share_id: str) -> list | None:
        """The whole conversation for its owner, oldest first, replies with their reasoning, steps (a figure count instead of
        the figures, as a kept chat holds them) and stats; None if the link is not theirs."""
        if self.get(owner, share_id) is None:
            return None
        with self.lock:
            rows = self.conn.execute("SELECT at, role, name, content, meta FROM share_messages WHERE share_id = ? ORDER BY id",
                                     (share_id,)).fetchall()
        return [{"at": at, "role": role, "name": name, "content": content, **self._extras(meta, None)}
                for at, role, name, content, meta in rows]

    def context(self, token: str) -> list:
        """The model's view of the conversation: its latest messages, finished ones only, each visitor's words tagged with
        their name when more than one person has spoken. Starts with a visitor's message and fits CONTEXT_CHARS. Earlier
        replies' tool steps are not in it: their answers carry them."""
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

    def view(self, share: dict, model: str | None, talk: bool, voice: dict, python: bool, library: bool) -> dict:
        """What a visitor's page is told about the link: nothing about the owner or the secret. `tools` are those the room
        offers (see room_tools), `effort` the owner's, `efforts` what a visitor may pick for their messages."""
        return {"title": share["title"], "model": model, "talk": talk, "expires": share["expires"],
                "messages_left": share["max_messages"] - share["messages"],
                "audio_left": share["max_audio"] - share["audio"], "speed": voice.get("speed", 1.0),
                "tools": room_tools(share, python, library), "effort": share["effort"], "efforts": list(EFFORTS)}
