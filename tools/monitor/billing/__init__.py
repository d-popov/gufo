"""Prepaid credits for Gufo API keys: accounts, USDC deposits, metering and key sync.

gufo-monitor loads this package only with --billing-config. `Billing` ties the parts
together: the database (db), prices (pricing), log metering and key-file sync (meter),
deposit checks on Solana and Base (chain, crypto) and the public portal (portal).
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .crypto import associated_token_address, b58decode
from .db import BillingError, Database
from .meter import KeySync, Meter
from .pricing import Pricing, PriceList, PricingError

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_CHAIN_ID = 8453
RECONCILE_S = 60.0
RECHECK_S = 30.0
PENDING_MAX_S = 24 * 3600.0
MAX_ADMIN_CREDIT = 10 ** 12
MAX_USAGE_DAYS = 365
MAX_DEPOSIT_ROWS = 1000
VERIFY_MAX = 50  # deposits re-fetched per verification run
VERIFY_PACE_S = 0.25  # between RPC checks of one run, for public RPC rate limits
VERIFY_GIVE_UP = 3  # consecutive unavailable answers end a run early
AUDIT_FIELDS = ("receiver_before_micro", "receiver_after_micro", "slot", "block_time")
CSV_COLUMNS = ("id", "time_utc", "user_id", "email", "chain", "tx", "sender", "amount_micro",
               "receiver_before_micro", "receiver_after_micro", "receiver_live_micro",
               "live_at_utc", "slot", "block_time_utc", "audit_note", "receiver_owner",
               "receiver_account", "asset", "identity", "explorer")
USAGE_FIELDS = ("requests", "prompt", "cached", "generated", "cost_micro")
SOLANA_SIGNATURE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{64,90}")
BASE_TX = re.compile(r"0x[0-9a-fA-F]{64}")
BASE_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


class ConfigError(ValueError):
    pass


def _int(value, name: str, default: int, minimum: int = 0) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name}: expected an integer >= {minimum}")
    return value


def _str(section: dict, name: str, where: str, default: str | None = None) -> str:
    value = section.get(name, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{name}: expected a non-empty string")
    return value


def check_url(value, name: str, path: bool = True) -> str:
    """An http(s) base URL without a trailing slash. PATH=False refuses a path: the
    portal's pages and links live at the root of its host."""
    if not isinstance(value, str):
        raise ConfigError(f"{name}: expected a URL")
    try:
        parts = urllib.parse.urlsplit(value.strip())
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        parts.port  # noqa: B018 (raises ValueError on a bad port)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"{name}: expected an http:// or https:// URL, got {value!r}")
    if parts.username or parts.password or parts.query or parts.fragment or (
            not path and parts.path.strip("/")):
        example = "https://gufo.example.com" + ("/v1" if path else "")
        raise ConfigError(f"{name}: expected a plain base URL like {example}, got {value!r}")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def load_config(path: Path, public_url: str | None = None, api_url: str | None = None,
                default_url: str = "http://127.0.0.1:8099") -> dict:
    """Read and check --billing-config. Relative paths are relative to the file.

    The public URL (verification links, wallet messages) is PUBLIC_URL (GUFO_PUBLIC_URL),
    else the config's optional `public_base_url`, else DEFAULT_URL; the API URL shown to
    users is API_URL (GUFO_API_URL), else `api_base_url`, else the public URL + /v1."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except ValueError as error:
        raise ConfigError(f"{path}: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    base = path.resolve().parent
    if public_url:
        public = check_url(public_url, "GUFO_PUBLIC_URL", path=False)
    else:
        public = check_url(data.get("public_base_url", default_url), "public_base_url",
                           path=False)
    if api_url:
        api = check_url(api_url, "GUFO_API_URL")
    else:
        api = check_url(data.get("api_base_url", public + "/v1"), "api_base_url")
    config = {
        "db": base / _str(data, "db", "config"),
        "pricing": base / _str(data, "pricing", "config"),
        "signup_grant_micro": _int(data.get("signup_grant_micro"), "signup_grant_micro",
                                   990_000),
        "public_base_url": public,
        "api_base_url": api,
        "max_keys_per_user": _int(data.get("max_keys_per_user"), "max_keys_per_user", 5, 1),
        "model": data.get("model") if isinstance(data.get("model"), str) else None,
        "solana": None,
        "base": None,
    }
    solana = data.get("solana")
    if solana is not None:
        if not isinstance(solana, dict):
            raise ConfigError("solana: expected an object")
        owner = _str(solana, "receiver_owner", "solana")
        mint = _str(solana, "mint", "solana", USDC_MINT)
        try:  # the canonical (associated) USDC account of receiver_owner
            canonical = associated_token_address(owner, mint)[0]
        except ValueError as error:
            raise ConfigError(f"solana.receiver_owner and solana.mint must be Solana "
                              f"addresses: {error}") from None
        # Optional: an explicit token account is used as it is (older non-associated ones).
        token_account = _str(solana, "receiver_token_account", "solana", canonical)
        try:
            valid = len(b58decode(token_account)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("solana.receiver_token_account: expected a Solana address")
        config["solana"] = {
            "rpc": _str(solana, "rpc", "solana", "https://api.mainnet-beta.solana.com"),
            "receiver_owner": owner,
            "receiver_token_account": token_account,
            "mint": mint,
            "commitment": _str(solana, "commitment", "solana", "finalized"),
        }
    chain_base = data.get("base")
    if chain_base is not None:
        if not isinstance(chain_base, dict):
            raise ConfigError("base: expected an object")
        receiver = _str(chain_base, "receiver", "base")
        usdc = _str(chain_base, "usdc", "base", BASE_USDC)
        if not BASE_ADDRESS.fullmatch(receiver) or not BASE_ADDRESS.fullmatch(usdc):
            raise ConfigError("base.receiver and base.usdc must be 0x addresses")
        config["base"] = {
            "rpc": _str(chain_base, "rpc", "base", "https://mainnet.base.org"),
            "receiver": receiver.lower(),
            "usdc": usdc.lower(),
            "confirmations": _int(chain_base.get("confirmations"), "base.confirmations", 12, 1),
        }
    return config


def chain_clients(config: dict) -> dict:
    """The deposit checkers of the enabled chains."""
    from . import chain
    clients = {}
    if config["solana"]:
        s = config["solana"]
        clients["solana"] = chain.Solana(s["rpc"], s["receiver_owner"],
                                         s["receiver_token_account"], s["mint"],
                                         s["commitment"])
    if config["base"]:
        b = config["base"]
        clients["base"] = chain.Base(b["rpc"], b["receiver"], b["usdc"], b["confirmations"])
    return clients


def checker_for(config: dict, chain_name: str, identity: tuple[str, str, str]):
    """A deposit checker for a stored receiver IDENTITY (owner, account, asset) on the
    configured RPC (RPC URL, commitment and confirmations are infrastructure, not
    identity); None when the chain has no configuration now."""
    from . import chain
    owner, account, asset = identity
    if chain_name == "solana" and config.get("solana"):
        s = config["solana"]
        return chain.Solana(s["rpc"], owner, account, asset, s["commitment"])
    if chain_name == "base" and config.get("base"):
        b = config["base"]
        return chain.Base(b["rpc"], account, asset, b["confirmations"])
    return None


def checker_identity(client) -> tuple[str, str, str] | None:
    """The (owner, account, asset) a checker verifies against, or None if it cannot say."""
    identity = getattr(client, "identity", None)
    if isinstance(identity, tuple) and len(identity) == 3 and all(
            isinstance(part, str) and part for part in identity):
        return identity
    return None


def identity_status(row) -> str:
    """"stored" when the deposit carries its receiver identity, else "config-fallback"
    (credited before identities were stored: checked against today's receiver)."""
    return "stored" if all(row[name] for name in ("receiver_owner", "receiver_account",
                                                  "asset")) else "config-fallback"


def normalize_tx(chain_name: str, tx) -> str | None:
    if not isinstance(tx, str):
        return None
    tx = tx.strip()
    if chain_name == "solana":
        return tx if SOLANA_SIGNATURE.fullmatch(tx) else None
    if chain_name == "base":
        return tx.lower() if BASE_TX.fullmatch(tx) else None
    return None


def normalize_sender(chain_name: str, sender) -> str | None:
    if not isinstance(sender, str) or not sender:
        return None
    return sender.lower() if chain_name == "base" else sender


def _audit_int(value) -> int | None:
    """VALUE when it is a plain non-negative integer (what the chain reported), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None


def usdc_text(micro: int) -> str:
    """1500000 -> "1.50", 1234567 -> "1.234567": exact, at least two decimals."""
    whole, frac = divmod(abs(micro), 1_000_000)
    digits = f"{frac:06d}".rstrip("0").ljust(2, "0")
    return f"{'-' if micro < 0 else ''}{whole}.{digits}"


def audit_note(before: int | None, after: int | None, credited: int,
               created: bool = False) -> str | None:
    """The deposit's audit note: "account created by this deposit" when its transaction
    created the receiving account, and the mismatch when the receiver's balance moved by
    other than the credit (several transfers in one transaction or block, fees); None
    when there is nothing to note."""
    notes = ["account created by this deposit"] if created else []
    if before is not None and after is not None and after - before != credited:
        notes.append(f"balance delta {usdc_text(after - before)} != credited "
                     f"{usdc_text(credited)}")
    return "; ".join(notes) or None


def explorer_url(chain_name: str, tx: str) -> str | None:
    base = {"solana": "https://solscan.io/tx/", "base": "https://basescan.org/tx/"}.get(chain_name)
    return base + urllib.parse.quote(tx, safe="") if base else None


def csv_cell(value):
    """A CSV cell: blank for None; text a spreadsheet would run as a formula gets a
    leading quote."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _utc(t) -> str:
    if t is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Billing:
    """The billing service. `keys` is the monitor's KeyStore for Gufo's key file and
    `served_model` supplies the default displayed model (overridden by config `model`),
    not a key restriction. `chains` maps "solana"/"base" to deposit checkers."""

    Error = BillingError  # raised by deposit() and admin_action(): .status, .code, .message

    def __init__(self, config: dict, keys, served_model=lambda: None,
                 chains: dict | None = None):
        self.config = config
        self.db = Database(config["db"])
        self.prices = PriceList(config["pricing"])
        self.chains = chain_clients(config) if chains is None else chains
        self._served_model = served_model
        self._verify_lock = threading.Lock()
        self.verify_pace_s = VERIFY_PACE_S
        # Builds re-verification checkers for stored receiver identities; replaceable in tests.
        self.checker_factory = lambda name, identity: checker_for(self.config, name, identity)
        self._checkers: dict[tuple, object] = {}
        self.sync = KeySync(self.db, keys)
        self.meter = Meter(self.db, self.prices, self.sync)

    @property
    def pricing(self) -> Pricing:
        """The live prices; a new object after each admin save."""
        return self.prices.current

    def model(self) -> str | None:
        return self.config.get("model") or self._served_model()

    def save_pricing(self, data) -> dict:
        """Replace the price table (admin); raises BillingError when it is invalid.
        Requests billed afterwards use it; ledger rows already written stay as they are."""
        try:
            return self.prices.save(data).to_dict()
        except PricingError as error:
            raise BillingError(400, "bad_pricing", str(error)) from None

    # -- hooks and background work

    def on_request(self, record: dict):
        """Called by the monitor for every completed request in Gufo's log."""
        try:
            self.meter.on_request(record)
        except Exception as error:  # metering must never stop the log tail
            print(f"gufo-monitor: billing: metering failed: {error}", file=sys.stderr)

    def reconcile(self) -> bool:
        return self.sync.reconcile()

    def run(self, stop: threading.Event):
        """Reconcile keys at start and every minute; re-check pending deposits."""
        next_reconcile = next_recheck = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            try:
                if now >= next_reconcile:
                    next_reconcile = now + RECONCILE_S
                    self.reconcile()
                    self.db.expire()
                if now >= next_recheck:
                    next_recheck = now + RECHECK_S
                    self.recheck_pending()
            except Exception as error:
                print(f"gufo-monitor: billing: {error}", file=sys.stderr)
            stop.wait(max(0.5, min(next_reconcile, next_recheck) - time.monotonic()))

    # -- deposits

    def deposit(self, user_id: int, chain_name, tx) -> dict:
        client = self.chains.get(chain_name) if isinstance(chain_name, str) else None
        if client is None:
            raise BillingError(400, "chain_disabled", "deposits on this chain are not enabled")
        tx = normalize_tx(chain_name, tx)
        if tx is None:
            raise BillingError(400, "bad_tx", "not a transaction id for this chain")
        user = self.db.user(user_id)
        if user is None or user["status"] != "approved":
            raise BillingError(403, "not_approved", "the account is not approved")
        now = time.time()
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM deposits WHERE chain=? AND tx=?",
                               (chain_name, tx)).fetchone()
            if row is not None and row["status"] == "credited":
                if row["user_id"] == user_id:
                    return {"status": "credited", "amount_micro": row["amount_micro"],
                            "reason": ""}
                return {"status": "rejected", "amount_micro": 0, "reason": "already_claimed"}
            if row is None:
                conn.execute("INSERT INTO deposits (chain, tx, user_id, status, created, updated)"
                             " VALUES (?, ?, ?, 'pending', ?, ?)",
                             (chain_name, tx, user_id, now, now))
            else:  # not credited yet: whoever submits it claims it, if the sender is theirs
                conn.execute("UPDATE deposits SET user_id=?, status='pending', updated=? "
                             "WHERE chain=? AND tx=?", (user_id, now, chain_name, tx))
        return self.check_deposit(chain_name, tx)

    def check_deposit(self, chain_name: str, tx: str) -> dict:
        """Ask the chain about a stored deposit and settle it: credited once, kept
        pending (for at most a day) or rejected."""
        client = self.chains[chain_name]
        identity = checker_identity(client)  # what this check verified against
        check = client.check(tx)
        live = live_at = None
        if check.status == "confirmed":  # outside the write: a network call
            live, live_at = self._live_balance(chain_name, client)
        now = time.time()
        credited = None
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM deposits WHERE chain=? AND tx=?",
                               (chain_name, tx)).fetchone()
            if row["status"] == "credited":
                return {"status": "credited", "amount_micro": row["amount_micro"], "reason": ""}
            user_id = row["user_id"]
            status, amount, reason = check.status, int(check.amount_micro or 0), check.reason or ""
            sender = normalize_sender(chain_name, check.sender)
            if status == "confirmed":
                linked = sender is not None and conn.execute(
                    "SELECT 1 FROM wallets WHERE chain=? AND address=? AND user_id=?",
                    (chain_name, sender, user_id)).fetchone()
                if not linked:
                    status, reason = "rejected", "sender_not_linked"
                elif amount <= 0:
                    status, reason = "rejected", "no_amount"
                else:
                    credited = Database.post(conn, user_id, "deposit", amount,
                                             f"{chain_name}:{tx}", f"{chain_name} from {sender}")
                    status, reason = "credited", ""
            elif status == "pending" and now - row["created"] > PENDING_MAX_S:
                status, reason = "rejected", "expired"
            elif status not in ("pending", "rejected"):
                status, reason = "rejected", f"unknown status {status}"
            audit = (None,) * 10
            if status == "credited":
                before = _audit_int(getattr(check, "receiver_before_micro", None))
                after = _audit_int(getattr(check, "receiver_after_micro", None))
                audit = (before, after, _audit_int(getattr(check, "slot", None)),
                         _audit_int(getattr(check, "block_time", None)),
                         live, live_at, audit_note(
                             before, after, amount,
                             getattr(check, "receiver_created", False) is True),
                         *(identity or (None, None, None)))
            conn.execute("UPDATE deposits SET status=?, amount_micro=?, sender=?, reason=?, "
                         "updated=?, receiver_before_micro=?, receiver_after_micro=?, slot=?, "
                         "block_time=?, receiver_live_micro=?, live_at=?, audit_note=?, "
                         "receiver_owner=?, receiver_account=?, asset=? "
                         "WHERE chain=? AND tx=?",
                         (status, amount if status == "credited" else 0, sender, reason[:200],
                          now, *audit, chain_name, tx))
        if credited is not None and credited[0] <= 0 < credited[1]:
            self.reconcile()
        return {"status": status, "amount_micro": amount if status == "credited" else 0,
                "reason": reason}

    def recheck_pending(self):
        rows = self.db.query("SELECT chain, tx FROM deposits WHERE status='pending'")
        for row in rows:
            if row["chain"] not in self.chains:
                continue
            try:
                self.check_deposit(row["chain"], row["tx"])
            except Exception as error:
                print(f"gufo-monitor: billing: deposit check failed: {error}", file=sys.stderr)

    @staticmethod
    def _live_balance(chain_name: str, client) -> tuple[int | None, float | None]:
        """The receiver's balance now and when it was read, or (None, None): a failed
        read never holds up a credit."""
        read = getattr(client, "receiver_balance", None)
        if read is None:
            return None, None
        try:
            value = _audit_int(read())
        except Exception as error:  # the transport is injectable: any failure is a miss
            print(f"gufo-monitor: billing: {chain_name} live balance: {error}", file=sys.stderr)
            return None, None
        return (value, time.time()) if value is not None else (None, None)

    # -- deposit audit (admin)

    def _credited(self, limit: int | None) -> list[dict]:
        rows = self.db.query(
            "SELECT d.rowid AS id, d.updated AS t, d.user_id, u.email, d.chain, d.tx, d.sender, "
            "d.amount_micro, d.receiver_before_micro, d.receiver_after_micro, "
            "d.receiver_live_micro, d.live_at, d.slot, d.block_time, d.audit_note, "
            "d.receiver_owner, d.receiver_account, d.asset "
            "FROM deposits d JOIN users u ON u.id=d.user_id WHERE d.status='credited' "
            "ORDER BY d.updated DESC, d.rowid DESC LIMIT ?", (-1 if limit is None else limit,))
        return [{**dict(row), "identity": identity_status(row),
                 "explorer": explorer_url(row["chain"], row["tx"])} for row in rows]

    def admin_deposits(self, limit: int = 200) -> dict:
        """The newest LIMIT credited deposits with their audit facts, and totals over all."""
        if isinstance(limit, bool) or not isinstance(limit, int) or \
                not 1 <= limit <= MAX_DEPOSIT_ROWS:
            raise BillingError(400, "bad_limit", f"limit must be an integer from 1 to "
                               f"{MAX_DEPOSIT_ROWS}")
        totals = self.db.one("SELECT COALESCE(SUM(amount_micro), 0) AS credited_micro, "
                             "COUNT(*) AS deposits FROM deposits WHERE status='credited'")
        return {"deposits": self._credited(limit), "totals": dict(totals)}

    def deposits_csv(self) -> str:
        """Every credited deposit as CSV for a spreadsheet, newest first. Text cells that
        a spreadsheet would read as a formula start with a quote."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        for row in self._credited(None):
            row["time_utc"] = _utc(row["t"])
            row["live_at_utc"] = _utc(row["live_at"])
            row["block_time_utc"] = _utc(row["block_time"])
            writer.writerow([csv_cell(row[name]) for name in CSV_COLUMNS])
        return out.getvalue()

    def verify_deposits(self, limit: int = VERIFY_MAX) -> dict:
        """Re-fetch the newest LIMIT credited deposits from the chain and compare them
        with what is stored. Read-only: credits and stored values never change. One run
        at a time, paced for public RPC limits; it stops early when the RPC keeps failing."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= VERIFY_MAX:
            raise BillingError(400, "bad_limit", f"limit must be an integer from 1 to "
                               f"{VERIFY_MAX}")
        if not self._verify_lock.acquire(blocking=False):
            raise BillingError(409, "busy", "a verification is already running")
        try:
            rows = self.db.query(
                "SELECT rowid AS id, chain, tx, sender, amount_micro, receiver_before_micro, "
                "receiver_after_micro, slot, block_time, receiver_owner, receiver_account, "
                "asset FROM deposits WHERE status='credited' "
                "ORDER BY updated DESC, rowid DESC LIMIT ?", (limit,))
            counts = dict.fromkeys(("ok", "mismatch", "unavailable", "skipped"), 0)
            results = []
            misses = 0
            asked = False
            for row in rows:
                identity = identity_status(row)
                client = self._verifier(row, identity)
                if client is None:
                    outcome, problems, notes = "unavailable", ["chain not enabled"], []
                elif misses >= VERIFY_GIVE_UP:
                    outcome, problems, notes = "skipped", ["stopped after repeated RPC failures"], []
                else:
                    if asked and self.verify_pace_s > 0:
                        time.sleep(self.verify_pace_s)
                    asked = True
                    outcome, problems, notes = self._verify_one(row, client)
                    misses = misses + 1 if outcome == "unavailable" else 0
                counts[outcome] += 1
                results.append({"id": row["id"], "chain": row["chain"], "tx": row["tx"],
                                "explorer": explorer_url(row["chain"], row["tx"]),
                                "identity": identity, "result": outcome, "problems": problems,
                                "notes": notes})
        finally:
            self._verify_lock.release()
        return {"checked": len(results), **counts, "results": results}

    def _verifier(self, row, identity: str):
        """The checker for a credited deposit: one for the receiver identity stored with it
        (today's checker when that is still the configured receiver), or today's checker
        for deposits from before identities were stored; None when unavailable."""
        current = self.chains.get(row["chain"])
        if identity != "stored":
            return current
        stored = (row["receiver_owner"], row["receiver_account"], row["asset"])
        if current is not None and checker_identity(current) == stored:
            return current
        key = (row["chain"], *stored)
        if key not in self._checkers:
            self._checkers[key] = self.checker_factory(row["chain"], stored)
        return self._checkers[key]

    @staticmethod
    def _verify_one(row, client) -> tuple[str, list[str], list[str]]:
        """"ok", "mismatch" or "unavailable", with the mismatches and other notes."""
        try:
            check = client.check(row["tx"])
        except Exception as error:
            return "unavailable", [f"check failed: {error}"], []
        if check.status == "pending":
            return "unavailable", [f"chain: {check.reason}"], []
        if check.status != "confirmed":
            return "mismatch", [f"chain now says {check.status}: {check.reason}"], []
        problems, notes = [], []
        sender = normalize_sender(row["chain"], check.sender)
        if sender != row["sender"]:
            problems.append(f"sender: stored {row['sender']}, chain {sender}")
        if int(check.amount_micro or 0) != row["amount_micro"]:
            problems.append(f"amount_micro: stored {row['amount_micro']}, "
                            f"chain {check.amount_micro}")
        for name in AUDIT_FIELDS:
            stored, fresh = row[name], _audit_int(getattr(check, name, None))
            if stored is not None and fresh is not None and stored != fresh:
                problems.append(f"{name}: stored {stored}, chain {fresh}")
            elif stored is None and fresh is not None:
                notes.append(f"{name}: not stored, chain {fresh}")
            elif stored is not None and fresh is None:
                notes.append(f"{name}: stored {stored}, chain gave none")
        return ("mismatch" if problems else "ok"), problems, notes

    # -- admin

    def verification_link(self, token: str) -> str:
        return (self.config["public_base_url"] + "/api/portal/verify?token="
                + urllib.parse.quote(token, safe=""))

    def admin_accounts(self) -> dict:
        rows = self.db.query(
            "SELECT u.*, "
            "(SELECT COUNT(*) FROM keys k WHERE k.user_id=u.id AND k.revoked=0) AS key_count, "
            "(SELECT COALESCE(-SUM(amount_micro), 0) FROM ledger l "
            " WHERE l.user_id=u.id AND l.kind='usage') AS spent, "
            "(SELECT COALESCE(SUM(amount_micro), 0) FROM ledger l "
            " WHERE l.user_id=u.id AND l.kind='deposit') AS deposited "
            "FROM users u ORDER BY u.id")
        wallets: dict[int, list] = {}
        for row in self.db.query("SELECT user_id, chain, address FROM wallets ORDER BY linked"):
            wallets.setdefault(row["user_id"], []).append(
                {"chain": row["chain"], "address": row["address"]})
        accounts = [{
            "id": row["id"],
            "email": row["email"],
            "status": row["status"],
            "email_verified": bool(row["email_verified"]),
            "verification_link": self.verification_link(row["verify_token"])
            if not row["email_verified"] and row["verify_token"] else None,
            "balance_micro": row["balance_micro"],
            "created": row["created"],
            "approved_at": row["approved_at"],
            "wallets": wallets.get(row["id"], []),
            "keys": row["key_count"],
            "spent_micro": row["spent"],
            "deposited_micro": row["deposited"],
        } for row in rows]
        totals = {
            "users": len(accounts),
            "pending_approval": sum(a["status"] == "pending_approval" for a in accounts),
            "balance_micro": sum(a["balance_micro"] for a in accounts),
            "deposited_micro": sum(a["deposited_micro"] for a in accounts),
            "spent_micro": sum(a["spent_micro"] for a in accounts),
        }
        return {"accounts": accounts, "totals": totals}

    def revenue_micro(self, hours: int, now: float | None = None) -> dict[str, int]:
        """Micro-dollars of the last HOURS hours: "users" is what portal users paid
        (usage ledger debits), "internal" the requests of keys outside the portal at
        the prices of the moment (never charged), "total" both."""
        now = time.time() if now is None else now
        since = now - hours * 3600
        users = self.db.one("SELECT COALESCE(-SUM(amount_micro), 0) AS micro FROM ledger "
                            "WHERE kind='usage' AND t>=?", (since,))["micro"]
        internal = self.db.one("SELECT COALESCE(SUM(cost_micro), 0) AS micro FROM usage_log "
                               "WHERE kind='internal' AND t>=?", (since,))["micro"]
        return {"users": int(users), "internal": int(internal), "total": int(users + internal)}

    def admin_usage(self, days: int, now: float | None = None) -> dict:
        """Usage of the last DAYS UTC calendar days, today included. "internal": the
        requests of keys outside the portal (the owner's and hand-made ones), never
        charged, priced when they were metered. "users": what portal users paid (usage
        ledger debits) with the tokens from usage_log; debits from before usage_log
        existed have no token counts and are counted in tokens_unknown_requests."""
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_USAGE_DAYS:
            raise BillingError(400, "bad_days", f"days must be an integer from 1 to "
                               f"{MAX_USAGE_DAYS}")
        now = time.time() if now is None else now
        since = (now // 86400 - (days - 1)) * 86400
        sums = ("COUNT(*) AS requests, COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(cached_tokens), 0) AS cached, "
                "COALESCE(SUM(generated_tokens), 0) AS generated, "
                "COALESCE(SUM(cost_micro), 0) AS cost_micro")
        day = "date(t, 'unixepoch')"  # UTC

        def internal(group: str) -> list[dict]:
            return [dict(row) for row in self.db.query(
                f"SELECT {group} AS name, {sums} FROM usage_log WHERE kind='internal' "
                f"AND t>=? GROUP BY {group} ORDER BY {group}", (since,))]

        by_key = [{"key": row.pop("name"), **row} for row in internal("key_name")]
        by_key.sort(key=lambda row: (-row["cost_micro"], row["key"]))
        internal_by_day = [{"day": row.pop("name"), **row} for row in internal(day)]

        # Users: tokens from usage_log, money from the ledger (cost-free requests have no
        # debit; debits without a usage_log row predate it).
        users: dict[str, dict] = {}
        empty = lambda: {**dict.fromkeys(USAGE_FIELDS, 0), "tokens_unknown_requests": 0}  # noqa: E731
        for row in self.db.query(
                f"SELECT {day} AS day, COUNT(*) AS requests, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(cached_tokens), 0) AS cached, "
                "COALESCE(SUM(generated_tokens), 0) AS generated "
                f"FROM usage_log WHERE kind='user' AND t>=? GROUP BY {day}", (since,)):
            entry = users.setdefault(row["day"], empty())
            for field in ("requests", "prompt", "cached", "generated"):
                entry[field] += row[field]
        for row in self.db.query(
                f"SELECT {day} AS day, -SUM(amount_micro) AS cost, "
                "SUM(NOT EXISTS (SELECT 1 FROM usage_log u WHERE u.ref=l.ref "
                "AND u.kind='user')) AS unknown "
                f"FROM ledger l WHERE kind='usage' AND t>=? GROUP BY {day}", (since,)):
            entry = users.setdefault(row["day"], empty())
            entry["cost_micro"] += row["cost"]
            entry["requests"] += row["unknown"]
            entry["tokens_unknown_requests"] += row["unknown"]
        users_by_day = [{"day": name, **users[name]} for name in sorted(users)]

        def totals(rows: list[dict], fields) -> dict:
            return {field: sum(row[field] for row in rows) for field in fields}

        return {
            "days": days,
            "since": since,
            "internal": {"totals": totals(internal_by_day, USAGE_FIELDS), "by_key": by_key,
                         "by_day": internal_by_day},
            "users": {"totals": totals(users_by_day,
                                       (*USAGE_FIELDS, "tokens_unknown_requests")),
                      "by_day": users_by_day},
        }

    def admin_action(self, user_id: int, action: str, body: dict | None) -> dict:
        """verify, approve, reject, suspend, unsuspend or credit; raises BillingError."""
        if action not in ("verify", "approve", "reject", "suspend", "unsuspend", "credit"):
            raise BillingError(404, "not_found", "unknown action")
        result: dict = {"ok": True}
        with self.db.write() as conn:
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise BillingError(404, "not_found", "no such account")
            status = user["status"]
            if action == "verify":  # what the verification link does, for the admin
                if user["email_verified"]:
                    raise BillingError(409, "already_verified", "the email is already verified")
                Database.mark_verified(conn, user)
            elif action == "approve":
                if not user["email_verified"]:
                    raise BillingError(409, "not_verified", "the email is not verified yet")
                if status == "suspended":
                    raise BillingError(409, "suspended", "unsuspend the account instead")
                conn.execute("UPDATE users SET status='approved', "
                             "approved_at=COALESCE(approved_at, ?) WHERE id=?",
                             (time.time(), user_id))
                grant = self.config["signup_grant_micro"]
                posted = grant > 0 and Database.post(conn, user_id, "signup_grant", grant,
                                                     f"user:{user_id}", "signup credit")
                result["grant_micro"] = grant if posted else 0
            elif action == "reject":
                conn.execute("UPDATE users SET status='rejected' WHERE id=?", (user_id,))
            elif action == "suspend":
                if status != "approved":
                    raise BillingError(409, "not_approved", "only approved accounts can be "
                                       "suspended")
                conn.execute("UPDATE users SET status='suspended' WHERE id=?", (user_id,))
            elif action == "unsuspend":
                if status != "suspended":
                    raise BillingError(409, "not_suspended", "the account is not suspended")
                conn.execute("UPDATE users SET status='approved' WHERE id=?", (user_id,))
            else:
                amount = (body or {}).get("amount_micro")
                note = (body or {}).get("note", "")
                if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0 \
                        or abs(amount) > MAX_ADMIN_CREDIT:
                    raise BillingError(400, "bad_amount", "amount_micro must be a non-zero "
                                       "integer")
                if not isinstance(note, str) or len(note) > 200:
                    raise BillingError(400, "bad_note", "note must be text of at most 200 "
                                       "characters")
                Database.post(conn, user_id, "admin_credit", amount,
                              "admin:" + secrets.token_hex(8), note.strip() or None)
            result["balance_micro"] = conn.execute(
                "SELECT balance_micro FROM users WHERE id=?", (user_id,)).fetchone()[0]
        self.reconcile()
        return result
