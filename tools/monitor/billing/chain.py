"""On-chain USDC deposit checks over JSON-RPC: Solana (SPL Token) and Base (ERC-20).

Every RPC goes through an injectable `post(url, payload) -> dict` so tests can
feed canned responses. Transport and RPC errors never reject a deposit: they
yield status "pending" so the caller retries later. A confirmed check also carries
the receiver's USDC balance before and after the deposit, for the owner's audit;
those are None whenever the chain cannot tell, and never change the verdict.
Python 3.11+ standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import urllib.request

from .crypto import b58decode

RPC_TIMEOUT_S = 10
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class ChainError(Exception):
    """An RPC call failed (transport, HTTP, JSON-RPC error or malformed reply)."""


@dataclass
class DepositCheck:
    status: str  # "confirmed" | "pending" | "rejected"
    sender: str | None
    amount_micro: int
    reason: str
    # Audit facts of a confirmed deposit, None when the chain does not give them: the
    # receiver's balance right before and after (Solana: the transaction's own token
    # balances; Base: balanceOf at blocks N-1 and N), the slot (Base: block number) and
    # its Unix time; receiver_created when the transaction created the receiving
    # account (Solana: no pre balance entry; before is then 0). Not compared: two checks
    # are equal when their verdicts are.
    receiver_before_micro: int | None = field(default=None, compare=False)
    receiver_after_micro: int | None = field(default=None, compare=False)
    slot: int | None = field(default=None, compare=False)
    block_time: int | None = field(default=None, compare=False)
    receiver_created: bool = field(default=False, compare=False)


def _pending(reason: str) -> DepositCheck:
    return DepositCheck("pending", None, 0, reason)


def _rejected(reason: str, sender: str | None = None) -> DepositCheck:
    return DepositCheck("rejected", sender, 0, reason)


def default_post(url: str, payload: dict) -> dict:
    """POST one JSON-RPC request and return the decoded JSON reply."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S) as resp:
        return json.loads(resp.read())


class _Rpc:
    def __init__(self, rpc_url: str, post):
        self.rpc_url = rpc_url
        self._post = post or default_post
        self._id = 0

    def call(self, method: str, params: list):
        """Result of one JSON-RPC call; raises ChainError on any failure."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            reply = self._post(self.rpc_url, payload)
        except Exception as exc:  # transport is injectable: any failure is a network error
            raise ChainError(f"{method}: {type(exc).__name__}: {exc}") from None
        if not isinstance(reply, dict):
            raise ChainError(f"{method}: malformed reply")
        if reply.get("error") is not None:
            err = reply["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise ChainError(f"{method}: rpc error: {msg}")
        if "result" not in reply:
            raise ChainError(f"{method}: reply without result")
        return reply["result"]


def _int_amount(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("bad amount")
    return int(value)


def _optional_int(value) -> int | None:
    """VALUE when it is a plain non-negative integer, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None


class Solana:
    def __init__(self, rpc_url: str, receiver_owner: str, receiver_token_account: str, mint: str,
                 commitment: str = "finalized", post=None):
        self.receiver_owner = receiver_owner
        self.receiver_token_account = receiver_token_account
        self.mint = mint
        self.commitment = commitment
        self._rpc = _Rpc(rpc_url, post)

    def check(self, signature: str) -> DepositCheck:
        try:
            if len(b58decode(signature)) != 64:
                return _rejected("bad_signature")
        except ValueError:
            return _rejected("bad_signature")
        try:
            tx = self._rpc.call("getTransaction", [signature, {
                "encoding": "jsonParsed",
                "commitment": self.commitment,
                "maxSupportedTransactionVersion": 0,
            }])
        except ChainError as exc:
            return _pending(f"rpc_error: {exc}")
        if tx is None:
            return _pending(f"not_found_at_{self.commitment}")
        try:
            return self._evaluate(tx)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            return _pending(f"malformed_transaction: {type(exc).__name__}: {exc}")

    def _evaluate(self, tx: dict) -> DepositCheck:
        meta = tx.get("meta")
        if meta is None:
            return _pending("transaction_without_meta")
        if meta.get("err") is not None:
            return _rejected(f"transaction_failed: {json.dumps(meta['err'])}")
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        loaded = meta.get("loadedAddresses") or {}
        if keys and not any(isinstance(k, dict) for k in tx["transaction"]["message"]["accountKeys"]):
            keys += list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
        pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
        post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
        if self.receiver_token_account not in keys:
            return _rejected("receiver_not_in_transaction")
        recv_i = keys.index(self.receiver_token_account)
        recv_entry = post.get(recv_i) or pre.get(recv_i)
        if recv_entry is None:
            return _rejected("receiver_not_token_account")
        if recv_entry.get("mint") != self.mint:
            return _rejected("wrong_mint")
        if recv_entry.get("owner") not in (None, self.receiver_owner):
            return _rejected("wrong_receiver_owner")

        def balance(table, i):
            entry = table.get(i)
            if entry is None or entry.get("mint") != self.mint:
                return 0
            return _int_amount(entry["uiTokenAmount"]["amount"])

        received = balance(post, recv_i) - balance(pre, recv_i)
        if received <= 0:
            return _rejected("no_usdc_received")
        senders = set()
        for i in set(pre) | set(post):
            if i == recv_i:
                continue
            if balance(post, i) < balance(pre, i):
                owner = (pre.get(i) or {}).get("owner")
                if not owner:
                    return _rejected("sender_owner_unknown")
                senders.add(owner)
        if not senders:
            return _rejected("no_sender")
        if len(senders) > 1:
            return _rejected("mixed_senders")

        def recorded(table, i):  # the receiver's own entry; None when of another mint
            entry = table.get(i)
            if entry is None or entry.get("mint") != self.mint:
                return None
            return _int_amount(entry["uiTokenAmount"]["amount"])

        after = recorded(post, recv_i)
        # No pre entry but a USDC post entry (owner checked above): this transaction
        # created the receiving account (e.g. CreateIdempotent first), so it held 0.
        created = recv_i not in pre and after is not None
        return DepositCheck("confirmed", senders.pop(), received, "ok",
                            receiver_before_micro=0 if created else recorded(pre, recv_i),
                            receiver_after_micro=after,
                            slot=_optional_int(tx.get("slot")),
                            block_time=_optional_int(tx.get("blockTime")),
                            receiver_created=created)

    def token_accounts(self, owner: str) -> list[str]:
        """Owner's token accounts for the configured mint, largest balance first."""
        result = self._rpc.call("getTokenAccountsByOwner", [
            owner, {"mint": self.mint}, {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        try:
            rows = []
            for item in result["value"]:
                amount = _int_amount(item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                rows.append((amount, item["pubkey"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainError(f"getTokenAccountsByOwner: malformed reply: {exc}") from None
        rows.sort(key=lambda r: -r[0])
        return [pubkey for _, pubkey in rows]

    def latest_blockhash(self) -> str:
        """A recent blockhash ("confirmed" commitment, so it stays valid longer)."""
        result = self._rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        try:
            blockhash = result["value"]["blockhash"]
        except (KeyError, TypeError) as exc:
            raise ChainError(f"getLatestBlockhash: malformed reply: {exc}") from None
        if not isinstance(blockhash, str):
            raise ChainError("getLatestBlockhash: malformed reply")
        return blockhash

    def account_exists(self, address: str) -> bool:
        """Whether ADDRESS holds an account ("confirmed" commitment): getAccountInfo
        answers null for one that was never created or was closed."""
        result = self._rpc.call("getAccountInfo", [
            address, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0},
                      "commitment": "confirmed"}])
        if not isinstance(result, dict) or "value" not in result:
            raise ChainError("getAccountInfo: malformed reply")
        return result["value"] is not None

    def receiver_balance(self) -> int:
        """The receiver token account's balance now, at the configured commitment."""
        result = self._rpc.call("getTokenAccountBalance", [
            self.receiver_token_account, {"commitment": self.commitment}])
        try:
            return _int_amount(result["value"]["amount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainError(f"getTokenAccountBalance: malformed reply: {exc}") from None


_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_TOPIC_ADDR_RE = re.compile(r"0x0{24}([0-9a-f]{40})")


class Base:
    def __init__(self, rpc_url: str, receiver: str, usdc: str, confirmations: int = 12, post=None):
        self.receiver = receiver.lower()
        self.usdc = usdc.lower()
        self.confirmations = confirmations
        self._rpc = _Rpc(rpc_url, post)

    def check(self, tx_hash: str) -> DepositCheck:
        if not isinstance(tx_hash, str) or not _TX_HASH_RE.fullmatch(tx_hash):
            return _rejected("bad_tx_hash")
        try:
            receipt = self._rpc.call("eth_getTransactionReceipt", [tx_hash])
            if receipt is None:
                return _pending("not_found")
            latest = int(self._rpc.call("eth_blockNumber", []), 16)
        except ChainError as exc:
            return _pending(f"rpc_error: {exc}")
        except (TypeError, ValueError) as exc:
            return _pending(f"malformed_reply: {exc}")
        try:
            block = int(receipt["blockNumber"], 16)
            if block + self.confirmations > latest:
                return _pending(f"confirmations {max(0, latest - block)}/{self.confirmations}")
            result = self._evaluate(receipt)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return _pending(f"malformed_receipt: {type(exc).__name__}: {exc}")
        if result.status == "confirmed":
            # Block granularity: other transfers to the receiver in block N show in the
            # delta too. Nodes without historic state answer errors: those stay None.
            result.slot = block
            result.receiver_before_micro = self._optional(lambda: self.receiver_balance(block - 1))
            result.receiver_after_micro = self._optional(lambda: self.receiver_balance(block))
            result.block_time = self._optional(lambda: self._block_time(block))
        return result

    @staticmethod
    def _optional(read):
        try:
            return read()
        except (ChainError, KeyError, TypeError, ValueError, AttributeError):
            return None

    def receiver_balance(self, block: int | None = None) -> int:
        """USDC balanceOf(receiver) at BLOCK, or at the latest block."""
        data = "0x70a08231" + "0" * 24 + self.receiver[2:]
        tag = "latest" if block is None else hex(block)
        result = self._rpc.call("eth_call", [{"to": self.usdc, "data": data}, tag])
        if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", result):
            raise ChainError("eth_call: malformed reply")
        return int(result, 16)

    def _block_time(self, block: int) -> int:
        header = self._rpc.call("eth_getBlockByNumber", [hex(block), False])
        return int(header["timestamp"], 16)

    def _evaluate(self, receipt: dict) -> DepositCheck:
        if receipt.get("status") != "0x1":
            return _rejected("transaction_reverted")
        senders = set()
        total = 0
        foreign_token = False
        for log in receipt.get("logs") or []:
            topics = [t.lower() for t in log.get("topics") or []]
            if log.get("removed") or len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
                continue
            to = _TOPIC_ADDR_RE.fullmatch(topics[2])
            if not to or "0x" + to.group(1) != self.receiver:
                continue
            if str(log.get("address", "")).lower() != self.usdc:
                foreign_token = True
                continue
            sender = _TOPIC_ADDR_RE.fullmatch(topics[1])
            if not sender:
                return _rejected("malformed_transfer_log")
            senders.add("0x" + sender.group(1))
            total += int(log["data"], 16)
        if not senders:
            return _rejected("wrong_token_contract" if foreign_token else "no_usdc_transfer_to_receiver")
        if len(senders) > 1:
            return _rejected("mixed_senders")
        sender = senders.pop()
        if total <= 0:
            return _rejected("zero_amount", sender)
        return DepositCheck("confirmed", sender, total, "ok")
