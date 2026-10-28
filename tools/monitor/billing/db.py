"""SQLite storage for accounts, sessions, the money ledger, keys, wallets, deposits and
the usage log.

One connection serves every thread behind one lock, so writes never race. Money is
integer micro-USD. The ledger is append-only (triggers refuse UPDATE and DELETE), and
each ledger row and the balance it changes are written in the same transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

STATUSES = ("pending_verification", "pending_approval", "approved", "rejected", "suspended")
LEDGER_KINDS = ("signup_grant", "deposit", "usage", "admin_credit")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 14, 8, 1
MIN_PASSWORD = 10
MAX_PASSWORD = 1024
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+")
BILLED_KEY = re.compile(r"u(\d{1,18})-(\d{1,9})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password TEXT NOT NULL,
  status TEXT NOT NULL,
  email_verified INTEGER NOT NULL DEFAULT 0,
  verify_token TEXT UNIQUE,
  balance_micro INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  approved_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  t REAL NOT NULL,
  kind TEXT NOT NULL,
  amount_micro INTEGER NOT NULL,
  balance_after_micro INTEGER NOT NULL,
  ref TEXT NOT NULL,
  detail TEXT,
  UNIQUE (kind, ref)
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, id);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
  BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
  BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS keys (
  name TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  n INTEGER NOT NULL,
  label TEXT NOT NULL,
  digest TEXT NOT NULL,
  created REAL NOT NULL,
  last_used REAL,
  revoked INTEGER NOT NULL DEFAULT 0,
  UNIQUE (user_id, n)
);
CREATE TABLE IF NOT EXISTS wallets (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id),
  linked REAL NOT NULL,
  PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS challenges (
  nonce TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  message TEXT NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deposits (
  chain TEXT NOT NULL,
  tx TEXT NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL,
  amount_micro INTEGER NOT NULL DEFAULT 0,
  sender TEXT,
  reason TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  updated REAL NOT NULL,
  PRIMARY KEY (chain, tx)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Every metered request with its tokens: "user" rows (portal keys, written with the
-- ledger debit) and "internal" rows (other keys: never charged, cost at the prices of
-- the moment). Not money: the ledger stays the record of what users paid.
CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  t REAL NOT NULL,
  key_name TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id),
  ref TEXT NOT NULL UNIQUE,
  model TEXT,
  prompt_tokens INTEGER NOT NULL,
  cached_tokens INTEGER NOT NULL,
  generated_tokens INTEGER NOT NULL,
  cost_micro INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_log_time ON usage_log(kind, t);
CREATE INDEX IF NOT EXISTS ledger_kind_time ON ledger(kind, t);
CREATE INDEX IF NOT EXISTS deposits_status_time ON deposits(status, updated);
"""

# Audit columns of deposits, added in place to databases from before them (old rows
# keep NULL): the receiver wallet's USDC balance before and after the deposit, from the
# chain (Solana: the transaction's token balances; Base: balanceOf at blocks N-1 and N),
# its slot (Base: block number) and Unix time, a live balance read when it was credited
# (not authoritative) and a note when the balance delta differs from the credit.
DEPOSIT_AUDIT_COLUMNS = (
    ("receiver_before_micro", "INTEGER"),
    ("receiver_after_micro", "INTEGER"),
    ("slot", "INTEGER"),
    ("block_time", "INTEGER"),
    ("receiver_live_micro", "INTEGER"),
    ("live_at", "REAL"),
    ("audit_note", "TEXT"),
)
# The receiver a credited deposit was verified against, from that checker (never from
# later config), so re-verification keeps working after the receiver changes:
# receiver_owner (Solana wallet / Base address), receiver_account (Solana token account /
# Base address) and asset (Solana mint / Base USDC contract). NULL for older credits.
DEPOSIT_IDENTITY_COLUMNS = (
    ("receiver_owner", "TEXT"),
    ("receiver_account", "TEXT"),
    ("asset", "TEXT"),
)


class BillingError(Exception):
    """A refused operation: HTTP status, stable error code and a message for people."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def normalize_email(email) -> str | None:
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if len(email) <= 254 and EMAIL.fullmatch(email) else None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n),
                                   r=int(r), p=int(p), dklen=len(digest) // 2)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


# Compared against when the email is unknown, so a login costs the same either way.
_DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(16))


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def key_owner(name) -> int | None:
    """The user id in a portal key name `u<id>-<n>`, else None (owner/monitor keys)."""
    match = BILLED_KEY.fullmatch(name) if isinstance(name, str) else None
    return int(match[1]) if match else None


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                    timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        """Add the deposit audit and identity columns a database lacks; a no-op once
        they are there."""
        with self.write() as conn:
            have = {row["name"] for row in conn.execute("PRAGMA table_info(deposits)")}
            for name, kind in DEPOSIT_AUDIT_COLUMNS + DEPOSIT_IDENTITY_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE deposits ADD COLUMN {name} {kind}")

    def close(self):
        with self.lock:
            self.conn.close()

    @contextlib.contextmanager
    def write(self):
        """One write transaction; rolled back on any exception."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    # -- money

    @staticmethod
    def post(conn, user_id: int, kind: str, amount: int, ref: str, detail: str | None = None,
             t: float | None = None) -> tuple[int, int] | None:
        """Append a ledger row and apply it to the balance, inside the caller's write
        transaction. Returns (balance before, balance after), or None when a row with
        this kind and ref exists already (a replay)."""
        if kind not in LEDGER_KINDS or isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("bad ledger entry")
        if conn.execute("SELECT 1 FROM ledger WHERE kind=? AND ref=?", (kind, ref)).fetchone():
            return None
        row = conn.execute("SELECT balance_micro FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError(user_id)
        before = row["balance_micro"]
        after = before + amount
        conn.execute("UPDATE users SET balance_micro=? WHERE id=?", (after, user_id))
        conn.execute(
            "INSERT INTO ledger (user_id, t, kind, amount_micro, balance_after_micro, ref, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, time.time() if t is None else t, kind, amount, after, ref, detail))
        return before, after

    @staticmethod
    def log_usage(conn, kind: str, user_id: int | None, t: float, key_name: str, ref: str,
                  model: str | None, prompt: int, cached: int, generated: int,
                  cost: int) -> bool:
        """Record one request in usage_log inside the caller's write transaction;
        False when its ref is there already (a replay)."""
        cursor = conn.execute(
            "INSERT OR IGNORE INTO usage_log (kind, t, key_name, user_id, ref, model, "
            "prompt_tokens, cached_tokens, generated_tokens, cost_micro) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, t, key_name, user_id, ref, model, prompt, cached, generated, cost))
        return cursor.rowcount == 1

    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    @staticmethod
    def set_meta(conn, key: str, value: str):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- accounts

    def signup(self, email: str, password: str) -> int | None:
        """Create an unverified account; None when the email is taken."""
        stored = hash_password(password)
        with self.write() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                return None
            cursor = conn.execute(
                "INSERT INTO users (email, password, status, verify_token, created) "
                "VALUES (?, ?, 'pending_verification', ?, ?)",
                (email, stored, secrets.token_urlsafe(32), time.time()))
            return cursor.lastrowid

    def verify_email(self, token) -> bool:
        """The verification link: marks the token's account verified; False for an
        unknown or used token."""
        if not isinstance(token, str) or not token or len(token) > 128:
            return False
        with self.write() as conn:
            row = conn.execute("SELECT id, status FROM users WHERE verify_token=?",
                               (token,)).fetchone()
            if row is None:
                return False
            self.mark_verified(conn, row)
            return True

    @staticmethod
    def mark_verified(conn, user: sqlite3.Row):
        """Within a write: the email of USER (a users row) is verified, which moves a
        pending_verification account on to pending_approval."""
        status = "pending_approval" if user["status"] == "pending_verification" \
            else user["status"]
        conn.execute("UPDATE users SET email_verified=1, verify_token=NULL, status=? "
                     "WHERE id=?", (status, user["id"]))

    def authenticate(self, email: str, password: str) -> int | None:
        row = self.one("SELECT id, password FROM users WHERE email=?", (email,))
        if row is None:
            check_password(password, _DUMMY_PASSWORD)
            return None
        return row["id"] if check_password(password, row["password"]) else None

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def set_status(self, conn, user_id: int, status: str):
        assert status in STATUSES
        conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))

    # -- sessions

    def create_session(self, user_id: int, max_age: float) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self.write() as conn:
            conn.execute("INSERT INTO sessions (id_hash, user_id, created, expires) "
                         "VALUES (?, ?, ?, ?)", (session_hash(session_id), user_id, now,
                                                 now + max_age))
        return session_id

    def session_user(self, session_id: str) -> int | None:
        row = self.one("SELECT user_id FROM sessions WHERE id_hash=? AND expires>?",
                       (session_hash(session_id), time.time()))
        return row["user_id"] if row else None

    def delete_session(self, session_id: str):
        with self.write() as conn:
            conn.execute("DELETE FROM sessions WHERE id_hash=?", (session_hash(session_id),))

    def expire(self):
        now = time.time()
        with self.write() as conn:
            conn.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            conn.execute("DELETE FROM challenges WHERE expires<=?", (now,))

    # -- keys

    def keys(self, user_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM keys WHERE user_id=? AND revoked=0 ORDER BY n",
                          (user_id,))

    def add_key(self, user_id: int, label: str, digest: str, limit: int) -> str:
        with self.write() as conn:
            active = conn.execute("SELECT COUNT(*) FROM keys WHERE user_id=? AND revoked=0",
                                  (user_id,)).fetchone()[0]
            if active >= limit:
                raise BillingError(409, "key_limit", f"at most {limit} keys per account")
            # Numbers are never reused, so late log lines still map to the right key.
            n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM keys WHERE user_id=?",
                             (user_id,)).fetchone()[0]
            name = f"u{user_id}-{n}"
            conn.execute("INSERT INTO keys (name, user_id, n, label, digest, created) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (name, user_id, n, label, digest, time.time()))
            return name

    def revoke_key(self, user_id: int, name: str) -> bool:
        with self.write() as conn:
            cursor = conn.execute("UPDATE keys SET revoked=1 WHERE user_id=? AND name=? AND "
                                  "revoked=0", (user_id, name))
            return cursor.rowcount > 0

    def wanted_keys(self) -> dict[str, str]:
        """Keys that belong in Gufo's key file: {name: digest} of approved, funded users."""
        rows = self.query("SELECT k.name, k.digest FROM keys k JOIN users u ON u.id=k.user_id "
                          "WHERE k.revoked=0 AND u.status='approved' AND u.balance_micro>0")
        return {row["name"]: row["digest"] for row in rows}

    # -- wallets

    def wallets(self, user_id: int) -> list[dict]:
        return [{"chain": row["chain"], "address": row["address"]} for row in self.query(
            "SELECT chain, address FROM wallets WHERE user_id=? ORDER BY linked", (user_id,))]
