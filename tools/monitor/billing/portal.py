"""The public portal's API: accounts, keys, wallets and deposits under /api/portal/.

The monitor's single listener serves the portal page and hands /api/portal/* here; these
routes never touch the monitor's data. State-changing requests need the
`X-Gufo-Portal: 1` header, which a cross-site page cannot send without a CORS
preflight, and a JSON body.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from . import crypto
from .db import MAX_PASSWORD, MIN_PASSWORD, BillingError, normalize_email


COOKIE_NAME = "gufo_portal"
COOKIE_MAX_AGE = 30 * 24 * 3600
PORTAL_HEADER = "X-Gufo-Portal"
CHALLENGE_TTL_S = 600.0
MIN_DEPOSIT, MAX_DEPOSIT = 1_000_000, 1_000_000_000
USDC_DECIMALS = 6
# What a top-up that creates the receiving USDC account costs the payer on top of the fee:
# rent-exempt minimum of a 165-byte token account.
RENT_NOTE = ("This first payment also creates the receiving USDC account: your wallet pays "
             "its one-time rent of about 0.00204 SOL on top of the network fee.")
BASE_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
BASE_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}")


def normalize_address(chain_name, address) -> str | None:
    """A wallet address in its stored form, or None when it is not one."""
    if not isinstance(address, str):
        return None
    address = address.strip()
    if chain_name == "base":
        return address.lower() if BASE_ADDRESS.fullmatch(address) else None
    if chain_name == "solana":
        try:
            return address if len(crypto.b58decode(address)) == 32 else None
        except Exception:
            return None
    return None


def verify_wallet_signature(chain_name: str, address: str, message: str, signature) -> bool:
    if not isinstance(signature, str):
        return False
    try:
        if chain_name == "solana":
            raw = crypto.b58decode(signature.strip())
            return len(raw) == 64 and crypto.ed25519_verify(
                crypto.b58decode(address), message.encode(), raw)
        if chain_name == "base":
            signature = signature.strip()
            if not BASE_SIGNATURE.fullmatch(signature):
                return False
            recovered = crypto.eth_recover_personal(message, signature)
            return isinstance(recovered, str) and recovered.lower() == address
    except Exception:
        return False
    return False


class Portal:
    """The portal's shared state. The monitor's listener serves its page and hands each
    /api/portal/* request to handle()."""

    def __init__(self, billing, secure_cookie: bool, limiter):
        self.billing = billing
        self.secure_cookie = secure_cookie
        self.limiter = limiter  # the monitor's sign-in RateLimiter, shared with admin sign-in
        self.receiver_exists = False  # the Solana receiving token account, once seen on chain

    def handle(self, handler, method: str):
        """Answer the /api/portal/* request of HANDLER, the monitor's request handler."""
        request = PortalRequest(handler, self)
        if method == "GET":
            request.get()
        else:
            request.mutation(method)


class PortalRequest:
    """One /api/portal/* request, answered through the monitor's request handler, which
    adds the security headers and logs failures without query strings."""

    server: Portal

    def __init__(self, handler, portal: Portal):
        self.handler = handler
        self.server = portal
        self.headers = handler.headers
        self.path = handler.path
        self.rfile = handler.rfile

    # -- plumbing

    def _send(self, status: int, body: bytes, content_type: str,
              headers: tuple[tuple[str, str], ...] = (), cache: str = "no-store"):
        self.handler._send(status, body, content_type, headers, cache)

    def _json(self, status: int, payload, headers: tuple[tuple[str, str], ...] = ()):
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(),
                   "application/json", headers)

    def _error(self, status: int, code: str, message: str = ""):
        self._json(status, {"error": code, "message": message or code})

    def _secure(self) -> bool:
        return self.server.secure_cookie or self.headers.get("X-Forwarded-Proto", "") == "https"

    def _cookie(self, value: str, max_age: int) -> tuple[str, str]:
        cookie = (f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
                  "SameSite=Strict")
        return ("Set-Cookie", cookie + ("; Secure" if self._secure() else ""))

    def _session_id(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
        return None

    def _user_id(self) -> int | None:
        session = self._session_id()
        return self.server.billing.db.session_user(session) if session else None

    def _body(self, limit: int = 16 << 10) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length == 0:
            return {}
        if length < 0 or length > limit:
            return None
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    # -- routing (the monitor's handler sends /api/portal/* here)

    def get(self):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        try:
            if path == "/api/portal/verify":
                return self._verify(urllib.parse.parse_qs(split.query).get("token", [""])[0])
            if path == "/api/portal/me":
                return self._me()
            if path == "/api/portal/ledger":
                return self._ledger()
            self._error(404, "not_found")
        except BillingError as error:
            self._error(error.status, error.code, error.message)

    def mutation(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        if self.headers.get(PORTAL_HEADER, "") != "1":
            return self._error(403, "csrf", f"the {PORTAL_HEADER} header is required")
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "expected a JSON object body")
        routes = {
            "/api/portal/signup": self._signup,
            "/api/portal/login": self._login,
            "/api/portal/logout": self._logout,
            "/api/portal/keys": self._key_create,
            "/api/portal/wallet/challenge": self._challenge,
            "/api/portal/wallet/link": self._link,
            "/api/portal/solana/prepare": self._solana_prepare,
            "/api/portal/deposit": self._deposit,
        }
        try:
            if method == "POST" and path in routes:
                return routes[path](body)
            if method == "DELETE" and path.startswith("/api/portal/keys/"):
                return self._key_delete(urllib.parse.unquote(path[len("/api/portal/keys/"):]))
            if path in routes or path.startswith("/api/portal/keys/"):
                return self._error(405, "method_not_allowed")
            self._error(404, "not_found")
        except BillingError as error:
            self._error(error.status, error.code, error.message)

    def _require_user(self):
        user_id = self._user_id()
        user = self.server.billing.db.user(user_id) if user_id is not None else None
        if user is None:
            raise BillingError(401, "unauthorized", "log in first")
        return user

    def _require_approved(self):
        user = self._require_user()
        if user["status"] != "approved":
            raise BillingError(403, "not_approved", "the account is not approved")
        return user

    # -- accounts

    def _signup(self, body: dict):
        if not self.server.limiter.allow("signup:" + self.handler.client_ip()):
            return self._error(429, "rate_limited", "too many attempts, try again in a minute")
        email = normalize_email(body.get("email"))
        password = body.get("password")
        if email is None:
            return self._error(400, "invalid_email", "enter a valid email address")
        if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            return self._error(400, "weak_password",
                               f"the password needs at least {MIN_PASSWORD} characters")
        self.server.billing.db.signup(email, password)  # same answer if the email is taken
        self._json(201, {"status": "pending_verification"})

    def _verify(self, token: str):
        ok = self.server.billing.db.verify_email(token)
        location = "/?verified=1" if ok else "/?verified=0"
        self._send(302, b"", "text/plain", (("Location", location),))

    def _login(self, body: dict):
        if not self.server.limiter.allow("login:" + self.handler.client_ip()):
            return self._error(429, "rate_limited", "too many attempts, try again in a minute")
        email = normalize_email(body.get("email"))
        password = body.get("password")
        db = self.server.billing.db
        user_id = None
        if email is not None and isinstance(password, str) and len(password) <= MAX_PASSWORD:
            user_id = db.authenticate(email, password)
        if user_id is None:
            return self._error(401, "bad_credentials", "wrong email or password")
        session = db.create_session(user_id, COOKIE_MAX_AGE)
        self._json(200, {"ok": True}, (self._cookie(session, COOKIE_MAX_AGE),))

    def _logout(self, body: dict):
        session = self._session_id()
        if session:
            self.server.billing.db.delete_session(session)
        self._json(200, {"ok": True}, (self._cookie("", 0),))

    def _me(self):
        user = self._require_user()
        billing = self.server.billing
        db = billing.db
        enabled = user["status"] == "approved" and user["balance_micro"] > 0
        chains = {}
        solana = billing.config.get("solana")
        if solana and "solana" in billing.chains:
            chains["solana"] = {"receiver_owner": solana["receiver_owner"],
                                "receiver_token_account": solana["receiver_token_account"],
                                "mint": solana["mint"], "decimals": USDC_DECIMALS}
        chain_base = billing.config.get("base")
        if chain_base and "base" in billing.chains:
            chains["base"] = {"receiver": chain_base["receiver"], "usdc": chain_base["usdc"],
                              "chain_id": 8453, "confirmations": chain_base["confirmations"]}
        model = billing.model()
        self._json(200, {
            "email": user["email"],
            "status": user["status"],
            "balance_micro": user["balance_micro"],
            "wallets": db.wallets(user["id"]),
            "keys": [{"name": k["name"], "label": k["label"], "created": k["created"],
                      "last_used": k["last_used"], "enabled": enabled}
                     for k in db.keys(user["id"])],
            # Portal keys are limited to MODEL, so its entry is what users pay.
            "pricing": dict(billing.pricing.for_model(model)),
            "model": model,
            "api_base_url": billing.config["api_base_url"],
            "signup_grant_micro": billing.config["signup_grant_micro"],
            "chains": chains,
        })

    def _ledger(self):
        user = self._require_user()
        rows = self.server.billing.db.query(
            "SELECT t, kind, amount_micro, balance_after_micro, ref, detail FROM ledger "
            "WHERE user_id=? ORDER BY id DESC LIMIT 200", (user["id"],))
        self._json(200, {"entries": [dict(row) for row in rows]})

    # -- keys

    def _key_create(self, body: dict):
        user = self._require_approved()
        label = body.get("label", "")
        if not isinstance(label, str) or len(label.strip()) > 64:
            return self._error(400, "bad_label", "the label must be at most 64 characters")
        billing = self.server.billing
        token = "gufo-" + secrets.token_urlsafe(30)
        digest = hashlib.sha256(token.encode()).hexdigest()
        name = billing.db.add_key(user["id"], label.strip(), digest,
                                  billing.config["max_keys_per_user"])
        billing.reconcile()
        self._json(201, {"name": name, "token": token})

    def _key_delete(self, name: str):
        user = self._require_user()
        billing = self.server.billing
        if not billing.db.revoke_key(user["id"], name):
            return self._error(404, "not_found", "no such key")
        billing.reconcile()
        self._json(200, {"ok": True})

    # -- wallets

    def _wallet_args(self, body: dict) -> tuple[str, str]:
        chain_name = body.get("chain")
        if chain_name not in self.server.billing.chains:
            raise BillingError(400, "chain_disabled", "this chain is not enabled")
        address = normalize_address(chain_name, body.get("address"))
        if address is None:
            raise BillingError(400, "bad_address", "not a wallet address for this chain")
        return chain_name, address

    def _challenge(self, body: dict):
        user = self._require_user()
        chain_name, address = self._wallet_args(body)
        billing = self.server.billing
        nonce = secrets.token_hex(16)
        now = time.time()
        domain = urllib.parse.urlsplit(billing.config["public_base_url"]).netloc
        issued = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = (f"{domain} asks you to link this wallet to your Gufo account "
                   f"({user['email']}).\n\n"
                   f"Address: {address}\nChain: {chain_name}\nNonce: {nonce}\n"
                   f"Issued At: {issued}")
        with billing.db.write() as conn:
            conn.execute("INSERT INTO challenges (nonce, user_id, chain, address, message, "
                         "expires) VALUES (?, ?, ?, ?, ?, ?)",
                         (nonce, user["id"], chain_name, address, message,
                          now + CHALLENGE_TTL_S))
        self._json(200, {"message": message})

    def _link(self, body: dict):
        user = self._require_user()
        chain_name, address = self._wallet_args(body)
        db = self.server.billing.db
        with db.write() as conn:  # single use: every challenge for this address goes now
            row = conn.execute(
                "SELECT message FROM challenges WHERE user_id=? AND chain=? AND address=? AND "
                "expires>? ORDER BY expires DESC LIMIT 1",
                (user["id"], chain_name, address, time.time())).fetchone()
            conn.execute("DELETE FROM challenges WHERE user_id=? AND chain=? AND address=?",
                         (user["id"], chain_name, address))
        if row is None:
            return self._error(400, "no_challenge", "ask for a new message to sign")
        if not verify_wallet_signature(chain_name, address, row["message"],
                                       body.get("signature")):
            return self._error(400, "bad_signature", "the signature does not match the wallet")
        with db.write() as conn:
            owner = conn.execute("SELECT user_id FROM wallets WHERE chain=? AND address=?",
                                 (chain_name, address)).fetchone()
            if owner is not None and owner["user_id"] != user["id"]:
                raise BillingError(409, "wallet_taken", "this wallet is linked to another "
                                   "account")
            if owner is None:
                conn.execute("INSERT INTO wallets (chain, address, user_id, linked) "
                             "VALUES (?, ?, ?, ?)", (chain_name, address, user["id"],
                                                     time.time()))
        self._json(200, {"ok": True})

    # -- deposits

    def _solana_prepare(self, body: dict):
        user = self._require_approved()
        billing = self.server.billing
        client = billing.chains.get("solana")
        if client is None:
            return self._error(400, "chain_disabled", "Solana deposits are not enabled")
        owner = normalize_address("solana", body.get("owner"))
        amount = body.get("amount_micro")
        if owner is None or billing.db.one(
                "SELECT 1 FROM wallets WHERE chain='solana' AND address=? AND user_id=?",
                (owner, user["id"])) is None:
            return self._error(403, "wallet_not_linked", "link this wallet first")
        if isinstance(amount, bool) or not isinstance(amount, int) or not (
                MIN_DEPOSIT <= amount <= MAX_DEPOSIT):
            return self._error(400, "bad_amount", "deposit between 1 and 1000 USDC")
        solana = billing.config["solana"]
        dest = solana["receiver_token_account"]
        try:
            accounts = client.token_accounts(owner)
            if not accounts:
                return self._error(400, "no_usdc_account", "this wallet holds no USDC")
            blockhash = client.latest_blockhash()
            # Once there, the account stays: only a missing one is asked about again.
            exists = self.server.receiver_exists or client.account_exists(dest)
        except Exception as error:
            print(f"gufo-monitor: portal: Solana RPC: {error}", file=sys.stderr)
            return self._error(502, "chain_unavailable", "the Solana RPC did not answer")
        create_for = None
        if exists:
            self.server.receiver_exists = True
        elif dest == crypto.associated_token_address(solana["receiver_owner"], solana["mint"])[0]:
            create_for = solana["receiver_owner"]  # the payment creates it first
        else:
            print(f"gufo-monitor: portal: solana.receiver_token_account {dest} does not exist",
                  file=sys.stderr)
            return self._error(503, "receiver_account_missing",
                               "the receiving USDC account does not exist; tell the admin")
        source = accounts[0]
        try:
            transaction = crypto.solana_transfer_checked_tx(
                owner, source, solana["mint"], dest, amount, USDC_DECIMALS, blockhash,
                create_dest_for=create_for)
        except ValueError as error:
            return self._error(400, "bad_transaction", str(error))
        reply = {"transaction": base64.b64encode(transaction).decode(),
                 "source_token_account": source, "creates_account": create_for is not None}
        if create_for is not None:
            reply["rent_note"] = RENT_NOTE
        self._json(200, reply)

    def _deposit(self, body: dict):
        user = self._require_approved()
        self._json(200, self.server.billing.deposit(user["id"], body.get("chain"),
                                                    body.get("tx")))
