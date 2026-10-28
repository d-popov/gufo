"""Usage metering from Gufo's request log, and key sync with Gufo's --api-key-file."""

from __future__ import annotations

import json
import sys
import threading

from .db import Database, key_owner
from .pricing import PriceList

HIGH_WATER = "last_metered_t"


def _tokens(record: dict, field: str) -> int:
    value = record.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class KeySync:
    """Keeps the portal keys (`u<id>-<n>`) in Gufo's key file equal to the keys of
    approved users with a positive balance. Other keys in the file are never touched.

    `store` is the monitor's KeyStore. Per-key restrictions live in the database;
    an empty model list allows every inference model, including future served models.
    """

    def __init__(self, db: Database, store):
        self.db = db
        self.store = store
        self._lock = threading.RLock()

    @staticmethod
    def managed_name(name) -> bool:
        return key_owner(name) is not None

    def reconcile(self, *, strict=False) -> bool:
        """Returns whether the file changed. Errors are reported and retried later."""
        with self._lock:  # a stale pass must not overwrite a newer one
            wanted = self.db.wanted_keys()
            try:
                return self.store.sync_managed(wanted, lambda name: key_owner(name) is not None)
            except (OSError, ValueError) as error:
                if strict:
                    raise
                print(f"gufo-monitor: billing key sync failed: {error}", file=sys.stderr)
                return False

    def admin_change(self, name: str, *, models=None, access="inference", revoke=False) -> bool:
        """Change a database-owned key, serialized with reconciliation.

        False means this is an ordinary file-owned key. Inactive managed keys can
        still be edited; credit restores their stored restrictions, not file defaults.
        """
        with self._lock:
            with self.db.write() as conn:
                key = conn.execute("SELECT revoked FROM keys WHERE name=?", (name,)).fetchone()
                if key is None:
                    return False
                if key["revoked"]:
                    raise LookupError(name)
                if revoke:
                    keys = self.store.list()
                    if len(keys) == 1 and keys[0]["name"] == name:
                        raise ValueError("cannot remove the last key")
                    conn.execute("UPDATE keys SET revoked=1 WHERE name=?", (name,))
                else:
                    if access != "inference":
                        raise ValueError("portal-managed keys must have inference access")
                    self.store._validated(name, access, models)
                    conn.execute("UPDATE keys SET models=? WHERE name=?",
                                 (json.dumps(models), name))
            self.reconcile(strict=True)
            return True


class Meter:
    """Debits each completed request of a portal key from its owner's balance, and
    records every keyed request in `usage_log` (tokens and cost) for the admin's usage
    view.

    A request is billed once: its ledger row is unique on (request id, log time),
    since Gufo restarts its request counter, and log lines older than the newest
    billed line are ignored, so a replayed log never debits twice. `usage_log` is
    unique on the same reference. Requests of other keys (the owner's, hand-made ones)
    are recorded as "internal" at the prices of the moment and never charged.
    """

    def __init__(self, db: Database, prices: PriceList, sync: KeySync):
        self.db = db
        self.prices = prices
        self.sync = sync

    def on_request(self, record: dict) -> int | None:
        """Meter one request row from the monitor's log tail; returns the micro-USD
        debited, or None when the row is not billable or was billed before."""
        name = record.get("key")
        request, t = record.get("request"), record.get("t")
        if not isinstance(name, str) or not name or not isinstance(request, str) \
                or not isinstance(t, (int, float)):
            return None
        owner = key_owner(name)
        prompt = _tokens(record, "prompt_tokens")
        cached = min(_tokens(record, "cached_tokens"), prompt)
        generated = _tokens(record, "generated_tokens")
        model = record.get("model") if isinstance(record.get("model"), str) else None
        cost = self.prices.current.cost(model, prompt, cached, generated)
        ref = f"{request}@{t:.3f}"
        usage = (t, name, ref, model, prompt, cached, generated, cost)
        if owner is None:
            with self.db.write() as conn:
                Database.log_usage(conn, "internal", None, *usage)
            return None
        detail = f"{name}: {prompt} prompt ({cached} cached), {generated} output"
        with self.db.write() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (HIGH_WATER,)).fetchone()
            if row is not None and t < float(row["value"]):
                return None
            key = conn.execute("SELECT user_id FROM keys WHERE name=?", (name,)).fetchone()
            if key is None or key["user_id"] != owner:
                return None
            if conn.execute("SELECT 1 FROM ledger WHERE kind='usage' AND ref=?", (ref,)).fetchone():
                return None
            conn.execute("UPDATE keys SET last_used=MAX(COALESCE(last_used, 0), ?) WHERE name=?",
                         (t, name))
            Database.set_meta(conn, HIGH_WATER, repr(float(max(t, float(row["value"]))
                                                           if row is not None else t)))
            Database.log_usage(conn, "user", owner, *usage)
            if cost <= 0:
                return 0
            change = Database.post(conn, owner, "usage", -cost, ref, detail, t=t)
        if change is None:
            return None
        before, after = change
        if before > 0 >= after:  # out of credit: take the user's keys out of Gufo's file
            self.sync.reconcile()
        return cost
