#!/usr/bin/env python3
"""CPU-only tests for tools/monitor/billing/{crypto,chain}.py.

Crypto is checked against independent oracles: published vectors, plus the
`cryptography` package (Ed25519 and secp256k1 signing) when it is installed.
Chain checks run against canned JSON-RPC replies through the injected `post`.
"""

import hashlib
import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/monitor"))

from billing import chain, crypto  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils as ec_utils
    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - oracle optional
    HAVE_CRYPTOGRAPHY = False

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def pk(seed: int) -> str:
    """Deterministic distinct base58 32-byte keys for tests."""
    return crypto.b58encode(hashlib.sha256(b"key%d" % seed).digest())


class Base58Test(unittest.TestCase):
    VECTORS = [
        ("", ""),
        ("00", "1"),
        ("0000", "11"),
        ("61", "2g"),
        ("626262", "a3gV"),
        ("636363", "aPEr"),
        ("516b6fcd0f", "ABnLTmg"),
        ("bf4f89001e670274dd", "3SEo3LWLoPntC"),
        ("572e4794", "3EFU7m"),
        ("ecac89cad93923c02321", "EJDM8drfXA6uyA"),
        ("10c8511e", "Rt5zm"),
        ("00000000000000000000", "1111111111"),
        (b"hello world".hex(), "StV1DL6CwTryKyV"),
        ("06ddf6e1d765a193d9cbe146ceeb79ac1cb485ed5f5b37913a8cf5857eff00a9", TOKEN_PROGRAM),
        ("00" * 32, "11111111111111111111111111111111"),
    ]

    def test_vectors(self):
        for hexstr, text in self.VECTORS:
            self.assertEqual(crypto.b58encode(bytes.fromhex(hexstr)), text)
            self.assertEqual(crypto.b58decode(text), bytes.fromhex(hexstr))

    def test_round_trip_and_invalid(self):
        for n in range(0, 70, 7):
            b = b"\0" * (n % 3) + os.urandom(n)
            self.assertEqual(crypto.b58decode(crypto.b58encode(b)), b)
        for bad in ("0", "O", "I", "l", "abc+"):
            with self.assertRaises(ValueError):
                crypto.b58decode(bad)


class KeccakTest(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(crypto.keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(crypto.keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(crypto.keccak256(b"Transfer(address,address,uint256)").hex(),
                         "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        self.assertEqual(crypto.keccak256(b"transfer(address,uint256)")[:4].hex(), "a9059cbb")

    def test_not_sha3_and_block_boundaries(self):
        self.assertNotEqual(crypto.keccak256(b""), hashlib.sha3_256(b"").digest())
        # Lengths around the 136-byte rate must all differ and be stable.
        digests = {crypto.keccak256(b"a" * n) for n in (134, 135, 136, 137, 271, 272, 273)}
        self.assertEqual(len(digests), 7)


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        # RFC 8032 section 7.1, TEST 1 and TEST 2.
        pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(crypto.ed25519_verify(pub, b"", sig))
        pub2 = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
        sig2 = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertTrue(crypto.ed25519_verify(pub2, b"\x72", sig2))
        self.assertFalse(crypto.ed25519_verify(pub2, b"\x73", sig2))
        self.assertFalse(crypto.ed25519_verify(pub, b"", sig2))

    def test_rejects_non_canonical_s_and_bad_lengths(self):
        pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        s = int.from_bytes(sig[32:], "little")
        malleated = sig[:32] + (s + crypto._L).to_bytes(32, "little")
        self.assertFalse(crypto.ed25519_verify(pub, b"", malleated))
        self.assertFalse(crypto.ed25519_verify(pub, b"", sig[:63]))
        self.assertFalse(crypto.ed25519_verify(pub[:31], b"", sig))
        # y >= p is a non-canonical point encoding.
        self.assertFalse(crypto.ed25519_verify(b"\xff" * 31 + b"\x7f", b"", sig))

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography not installed")
    def test_against_cryptography(self):
        for n in range(8):
            key = ed25519.Ed25519PrivateKey.generate()
            pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            msg = os.urandom(n * 37)
            sig = key.sign(msg)
            self.assertTrue(crypto.ed25519_verify(pub, msg, sig))
            bad = bytearray(sig)
            bad[n] ^= 1
            self.assertFalse(crypto.ed25519_verify(pub, msg, bytes(bad)))
            self.assertFalse(crypto.ed25519_verify(pub, msg + b"x", sig))


class EthRecoverTest(unittest.TestCase):
    def test_known_vectors(self):
        # Private key 1 -> well-known address.
        x, y = crypto._k_affine(crypto._k_mul(1, (crypto._K_G[0], crypto._K_G[1], 1)))
        self.assertEqual(crypto.eth_address_from_pubkey(x, y), "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf")
        # eth_account documentation example (sign_message, "I♥SF").
        sig = ("0xe6ca9bba58c88611fad66a6ce8f996908195593807c4b38bd528d2cff09d4eb3"
               "3e5bfbbf4d3e39b1a2fd816a7680c19ebebaf3a141b239934ad43cb33fcec8ce1c")
        addr = "0x5ce9454909639d2d17a3f753ce7d93fa0b9ab12e"
        self.assertEqual(crypto.eth_recover_personal("I♥SF", sig), addr)
        # v as 0/1 instead of 27/28, and no 0x prefix.
        self.assertEqual(crypto.eth_recover_personal("I♥SF", sig[2:-2] + "01"), addr)
        self.assertNotEqual(crypto.eth_recover_personal("I<3SF", sig), addr)
        self.assertNotEqual(crypto.eth_recover_personal("I♥SF", sig[:-2] + "1b"), addr)

    def test_malformed(self):
        for bad in ("", "0x", "0x1234", "zz" * 65, "0x" + "00" * 65, "0x" + "11" * 64 + "05"):
            self.assertIsNone(crypto.eth_recover_personal("hi", bad))

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography not installed")
    def test_against_cryptography(self):
        n = crypto._K_N
        for i in range(4):
            key = ec.generate_private_key(ec.SECP256K1())
            nums = key.public_key().public_numbers()
            expected = crypto.eth_address_from_pubkey(nums.x, nums.y)
            msg = f"Link wallet {i}\nnonce: {os.urandom(8).hex()}"
            data = msg.encode()
            digest = crypto.keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
            r, s = ec_utils.decode_dss_signature(
                key.sign(digest, ec.ECDSA(ec_utils.Prehashed(hashes.SHA256()))))
            if s > n // 2:
                s = n - s
            body = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            got = {v: crypto.eth_recover_personal(msg, "0x" + (body + bytes([v])).hex()) for v in (27, 28)}
            self.assertEqual(sum(1 for a in got.values() if a == expected), 1, got)


def compact_u16(buf: bytes, off: int):
    value = shift = 0
    while True:
        byte = buf[off]
        off += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, off


def decode_legacy_tx(raw: bytes) -> dict:
    """Reference decoder for the Solana legacy transaction wire format."""
    nsig, off = compact_u16(raw, 0)
    sigs = [raw[off + 64 * i: off + 64 * (i + 1)] for i in range(nsig)]
    off += 64 * nsig
    message = raw[off:]
    header = tuple(raw[off:off + 3])
    off += 3
    nkeys, off = compact_u16(raw, off)
    keys = [crypto.b58encode(raw[off + 32 * i: off + 32 * (i + 1)]) for i in range(nkeys)]
    off += 32 * nkeys
    blockhash = crypto.b58encode(raw[off:off + 32])
    off += 32
    ninstr, off = compact_u16(raw, off)
    instrs = []
    for _ in range(ninstr):
        prog = raw[off]
        off += 1
        nacc, off = compact_u16(raw, off)
        accs = list(raw[off:off + nacc])
        off += nacc
        ndata, off = compact_u16(raw, off)
        instrs.append((prog, accs, raw[off:off + ndata]))
        off += ndata
    assert off == len(raw), "trailing bytes"
    return {"sigs": sigs, "message": message, "header": header, "keys": keys,
            "blockhash": blockhash, "instructions": instrs}


class SolanaTxTest(unittest.TestCase):
    def build(self, **kw):
        args = dict(owner=pk(1), source_token_account=pk(2), mint=USDC_MINT,
                    dest_token_account=pk(3), amount=12_340_000, decimals=6, recent_blockhash=pk(9))
        args.update(kw)
        return args, crypto.solana_transfer_checked_tx(**args)

    def test_layout(self):
        args, raw = self.build()
        tx = decode_legacy_tx(raw)
        self.assertEqual(tx["sigs"], [b"\0" * 64])
        num_sig, ro_signed, ro_unsigned = tx["header"]
        self.assertEqual((num_sig, ro_signed, ro_unsigned), (1, 0, 2))
        keys = tx["keys"]
        self.assertEqual(keys[0], args["owner"])  # fee payer, signer, writable
        self.assertEqual(keys[-1], TOKEN_PROGRAM)  # read-only program last
        self.assertEqual(set(keys[1:3]), {args["source_token_account"], args["dest_token_account"]})
        writable = keys[:len(keys) - ro_unsigned]
        self.assertNotIn(args["mint"], writable)
        self.assertNotIn(TOKEN_PROGRAM, writable)
        self.assertEqual(tx["blockhash"], args["recent_blockhash"])
        self.assertEqual(len(tx["instructions"]), 1)
        prog, accs, data = tx["instructions"][0]
        self.assertEqual(keys[prog], TOKEN_PROGRAM)
        self.assertEqual([keys[i] for i in accs],
                         [args["source_token_account"], args["mint"], args["dest_token_account"], args["owner"]])
        tag, amount, decimals = struct.unpack("<BQB", data)
        self.assertEqual((tag, amount, decimals), (12, 12_340_000, 6))

    def test_large_amount_and_validation(self):
        _, raw = self.build(amount=2**64 - 1)
        _, _, data = decode_legacy_tx(raw)["instructions"][0]
        self.assertEqual(struct.unpack("<BQB", data)[1], 2**64 - 1)
        for bad in (dict(amount=0), dict(amount=2**64), dict(amount=1.5), dict(decimals=256),
                    dict(owner="notbase58!"), dict(mint=crypto.b58encode(b"short")),
                    dict(dest_token_account=pk(2))):
            with self.assertRaises(ValueError, msg=bad):
                self.build(**bad)

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography not installed")
    def test_signable_by_wallet_key(self):
        key = ed25519.Ed25519PrivateKey.generate()
        owner_raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        _, raw = self.build(owner=crypto.b58encode(owner_raw))
        message = decode_legacy_tx(raw)["message"]
        self.assertEqual(raw[1 + 64:], message)
        self.assertTrue(crypto.ed25519_verify(owner_raw, message, key.sign(message)))

    def test_create_receiver_account_layout(self):
        wallet = NEW_RECEIVER_OWNER
        ata = crypto.associated_token_address(wallet, USDC_MINT)[0]
        args, raw = self.build(dest_token_account=ata, create_dest_for=wallet)
        tx = decode_legacy_tx(raw)
        self.assertEqual(tx["sigs"], [b"\0" * 64])
        keys = tx["keys"]
        self.assertEqual(keys, [args["owner"], args["source_token_account"], ata, USDC_MINT,
                                TOKEN_PROGRAM, wallet, SYSTEM_PROGRAM, ATA_PROGRAM])
        num_sig, ro_signed, ro_unsigned = tx["header"]
        self.assertEqual((num_sig, ro_signed, ro_unsigned), (1, 0, 5))
        writable = keys[:len(keys) - ro_unsigned]
        self.assertEqual(writable, [args["owner"], args["source_token_account"], ata])
        self.assertEqual(len(tx["instructions"]), 2)
        (prog, accs, data), (prog2, accs2, data2) = tx["instructions"]
        self.assertEqual(keys[prog], ATA_PROGRAM)
        self.assertEqual([keys[i] for i in accs],  # payer, account, wallet, mint, system, token
                         [args["owner"], ata, wallet, USDC_MINT, SYSTEM_PROGRAM, TOKEN_PROGRAM])
        self.assertEqual(data, b"\x01")  # CreateIdempotent
        self.assertEqual(keys[prog2], TOKEN_PROGRAM)
        self.assertEqual([keys[i] for i in accs2],
                         [args["source_token_account"], USDC_MINT, ata, args["owner"]])
        self.assertEqual(struct.unpack("<BQB", data2), (12, 12_340_000, 6))
        # Only the wallet's canonical account may be created; the wallet is not the payer.
        with self.assertRaises(ValueError):
            self.build(dest_token_account=pk(3), create_dest_for=wallet)
        with self.assertRaises(ValueError):
            payer_ata = crypto.associated_token_address(pk(1), USDC_MINT)[0]
            self.build(dest_token_account=payer_ata, create_dest_for=pk(1))


NEW_RECEIVER_OWNER = "6Rpvu3zEcUK1QWLqZm8MhMKTZZCqc1qLqQEQmisWGeN9"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


class AssociatedTokenAddressTest(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(crypto.associated_token_address(
            "7QXGLRjvyFAmxdRaP9Wk18KwWTMfspF4Na2sr3o3PzxV", USDC_MINT),
            ("5rYBCPjppwZntGzmqMUgC1NVnSc2JdeWXLbZHssE1UyY", 254))
        self.assertEqual(crypto.associated_token_address(NEW_RECEIVER_OWNER, USDC_MINT),
                         ("C3LHV6bZdTM3k6VZxbrdaufqCvMLiZo3AKDYL4n8GbdZ", 250))
        self.assertEqual((crypto.TOKEN_PROGRAM_ID, crypto.ASSOCIATED_TOKEN_PROGRAM_ID,
                          crypto.SYSTEM_PROGRAM_ID), (TOKEN_PROGRAM, ATA_PROGRAM, SYSTEM_PROGRAM))
        self.assertEqual(crypto.b58decode(SYSTEM_PROGRAM), b"\0" * 32)

    def pda_candidate(self, owner, bump):
        seeds = (crypto.b58decode(owner) + crypto.b58decode(TOKEN_PROGRAM)
                 + crypto.b58decode(USDC_MINT) + bytes([bump]))
        return hashlib.sha256(seeds + crypto.b58decode(ATA_PROGRAM) + b"ProgramDerivedAddress").digest()

    def test_on_curve_is_solanas_decompression_not_the_subgroup_check(self):
        # Bump 255 of this owner is a curve point (outside the prime-order subgroup, which
        # libsodium would reject), so the address is bump 254.
        owner = "7QXGLRjvyFAmxdRaP9Wk18KwWTMfspF4Na2sr3o3PzxV"
        on_curve = self.pda_candidate(owner, 255)
        self.assertTrue(crypto.solana_on_curve(on_curve))
        self.assertFalse(crypto.solana_on_curve(self.pda_candidate(owner, 254)))
        y = int.from_bytes(on_curve, "little") & ((1 << 255) - 1)
        x = crypto._ed_recover_x(y % crypto._P, 0)
        point = (x, y % crypto._P, 1, x * y % crypto._P)
        self.assertFalse(crypto._ed_equal(crypto._ed_mul(crypto._L, point), (0, 1, 1, 0)))
        for bump in range(251, 256):  # NEW_RECEIVER_OWNER's earlier bumps are all on curve
            self.assertTrue(crypto.solana_on_curve(self.pda_candidate(NEW_RECEIVER_OWNER, bump)))
        # Real keys are points; the sign bit and y >= p (reduced) do not matter.
        self.assertTrue(crypto.solana_on_curve(crypto.b58decode(NEW_RECEIVER_OWNER)))
        base = (crypto._G_Y).to_bytes(32, "little")
        self.assertTrue(crypto.solana_on_curve(base[:31] + bytes([base[31] | 0x80])))
        self.assertTrue(crypto.solana_on_curve((1 + crypto._P).to_bytes(32, "little")))  # y = 1

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography not installed")
    def test_wallet_keys_are_on_curve(self):
        for _ in range(8):
            raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.assertTrue(crypto.solana_on_curve(raw))


class FakeRpc:
    """Canned JSON-RPC transport: method -> result, Exception, or callable(params)."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload["method"], payload["params"]))
        reply = self.responses[payload["method"]]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(payload["params"])
        if isinstance(reply, dict) and "error" in reply:
            return {"jsonrpc": "2.0", "id": payload["id"], "error": reply["error"]}
        return {"jsonrpc": "2.0", "id": payload["id"], "result": reply}


SOL_SIG = crypto.b58encode(b"\x07" * 64)
SENDER, SENDER_ATA = pk(10), pk(11)
RECV_OWNER, RECV_ATA = pk(20), pk(21)
OTHER, OTHER_ATA = pk(30), pk(31)


def tb(index, amount, owner, mint=USDC_MINT):
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": TOKEN_PROGRAM,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6, "uiAmountString": str(amount / 1e6)}}


def sol_tx(pre, post, err=None, keys=None):
    keys = keys or [SENDER, SENDER_ATA, RECV_ATA, USDC_MINT, TOKEN_PROGRAM]
    return {
        "slot": 1,
        "meta": {"err": err, "preTokenBalances": pre, "postTokenBalances": post},
        "transaction": {"signatures": [SOL_SIG], "message": {"accountKeys": [
            {"pubkey": k, "signer": i == 0, "writable": i < 3, "source": "transaction"}
            for i, k in enumerate(keys)]}},
    }


def good_sol_tx(amount=5_000_000):
    return sol_tx(pre=[tb(1, 9_000_000, SENDER), tb(2, 100, RECV_OWNER)],
                  post=[tb(1, 9_000_000 - amount, SENDER), tb(2, 100 + amount, RECV_OWNER)])


class SolanaCheckTest(unittest.TestCase):
    def solana(self, rpc):
        return chain.Solana("http://sol", RECV_OWNER, RECV_ATA, USDC_MINT, post=rpc)

    def check(self, result):
        return self.solana(FakeRpc(getTransaction=result)).check(SOL_SIG)

    def test_confirmed(self):
        rpc = FakeRpc(getTransaction=good_sol_tx())
        got = self.solana(rpc).check(SOL_SIG)
        self.assertEqual(got, chain.DepositCheck("confirmed", SENDER, 5_000_000, "ok"))
        url, method, params = rpc.calls[0]
        self.assertEqual((url, method, params[0]), ("http://sol", "getTransaction", SOL_SIG))
        self.assertEqual(params[1]["encoding"], "jsonParsed")
        self.assertEqual(params[1]["commitment"], "finalized")
        self.assertEqual(params[1]["maxSupportedTransactionVersion"], 0)

    def test_confirmed_with_string_keys_and_new_receiver_account(self):
        tx = sol_tx(pre=[tb(1, 9_000_000, SENDER)],
                    post=[tb(1, 0, SENDER), tb(2, 9_000_000, RECV_OWNER)])
        tx["transaction"]["message"]["accountKeys"] = [SENDER, SENDER_ATA, RECV_ATA, USDC_MINT, TOKEN_PROGRAM]
        self.assertEqual(self.check(tx), chain.DepositCheck("confirmed", SENDER, 9_000_000, "ok"))

    def test_pending(self):
        self.assertEqual(self.check(None).status, "pending")
        self.assertEqual(self.check(OSError("connection refused")).status, "pending")
        self.assertEqual(self.check(TimeoutError("timed out")).status, "pending")
        got = self.check({"error": {"code": 429, "message": "Too many requests"}})
        self.assertEqual(got.status, "pending")
        self.assertIn("Too many requests", got.reason)
        self.assertEqual(self.solana(lambda u, p: "garbage").check(SOL_SIG).status, "pending")
        self.assertEqual(self.check({"meta": None, "transaction": {}}).status, "pending")

    def test_rejected(self):
        cases = {
            "failed": sol_tx(pre=[tb(1, 9, SENDER), tb(2, 0, RECV_OWNER)],
                             post=[tb(1, 4, SENDER), tb(2, 5, RECV_OWNER)],
                             err={"InstructionError": [0, {"Custom": 1}]}),
            "wrong_mint": sol_tx(pre=[tb(1, 9, SENDER, mint=pk(99)), tb(2, 0, RECV_OWNER, mint=pk(99))],
                                 post=[tb(1, 4, SENDER, mint=pk(99)), tb(2, 5, RECV_OWNER, mint=pk(99))]),
            "wrong_receiver": sol_tx(pre=[tb(1, 9, SENDER), tb(2, 0, OTHER)],
                                     post=[tb(1, 4, SENDER), tb(2, 5, OTHER)],
                                     keys=[SENDER, SENDER_ATA, OTHER_ATA, USDC_MINT, TOKEN_PROGRAM]),
            "wrong_receiver_owner": sol_tx(pre=[tb(1, 9, SENDER), tb(2, 0, OTHER)],
                                           post=[tb(1, 4, SENDER), tb(2, 5, OTHER)]),
            "no_increase": sol_tx(pre=[tb(1, 9, SENDER), tb(2, 5, RECV_OWNER)],
                                  post=[tb(1, 9, SENDER), tb(2, 5, RECV_OWNER)]),
            "mixed_senders": sol_tx(
                pre=[tb(1, 9, SENDER), tb(2, 0, RECV_OWNER), tb(5, 9, OTHER)],
                post=[tb(1, 4, SENDER), tb(2, 10, RECV_OWNER), tb(5, 4, OTHER)],
                keys=[SENDER, SENDER_ATA, RECV_ATA, USDC_MINT, TOKEN_PROGRAM, OTHER_ATA]),
        }
        for name, tx in cases.items():
            got = self.check(tx)
            self.assertEqual(got.status, "rejected", name)
            self.assertEqual(got.amount_micro, 0, name)
        self.assertIn("mixed_senders", self.check(cases["mixed_senders"]).reason)
        self.assertIn("wrong_mint", self.check(cases["wrong_mint"]).reason)
        self.assertIn("failed", self.check(cases["failed"]).reason)
        bad_sig = self.solana(FakeRpc()).check("not-a-signature")
        self.assertEqual((bad_sig.status, bad_sig.reason), ("rejected", "bad_signature"))
        self.assertEqual(self.solana(FakeRpc()).check(crypto.b58encode(b"\1" * 32)).status, "rejected")

    def test_other_mint_balances_ignored(self):
        tx = good_sol_tx()
        tx["meta"]["preTokenBalances"].append(tb(4, 50, OTHER, mint=pk(77)))
        tx["meta"]["postTokenBalances"].append(tb(4, 10, OTHER, mint=pk(77)))
        tx["transaction"]["message"]["accountKeys"].append({"pubkey": pk(78), "signer": False, "writable": True})
        self.assertEqual(self.check(tx).status, "confirmed")

    def test_audit_balances_from_the_transaction(self):
        tx = good_sol_tx(5_000_000)
        tx["slot"], tx["blockTime"] = 321_000_000, 1_800_000_000
        got = self.check(tx)
        self.assertEqual((got.status, got.receiver_before_micro, got.receiver_after_micro,
                          got.slot, got.block_time), ("confirmed", 100, 5_000_100, 321_000_000,
                                                     1_800_000_000))
        # Not part of equality: verdicts compare as before.
        self.assertEqual(got, chain.DepositCheck("confirmed", SENDER, 5_000_000, "ok"))

    def test_transfer_that_created_the_receiver_account(self):
        # CreateIdempotent then TransferChecked: no pre balance entry for the receiver.
        tx = sol_tx(pre=[tb(1, 9_000_000, SENDER)], post=[tb(1, 0, SENDER), tb(2, 9_000_000, RECV_OWNER)])
        got = self.check(tx)
        self.assertEqual((got.status, got.sender, got.amount_micro, got.receiver_before_micro,
                          got.receiver_after_micro, got.receiver_created),
                         ("confirmed", SENDER, 9_000_000, 0, 9_000_000, True))
        self.assertEqual(got.slot, 1)
        self.assertIsNone(got.block_time)  # no blockTime in the reply
        self.assertFalse(self.check(good_sol_tx()).receiver_created)
        # Created, but for someone else or of another mint: still refused.
        for reason, entry in (("wrong_receiver_owner", tb(2, 9_000_000, OTHER)),
                              ("wrong_mint", tb(2, 9_000_000, RECV_OWNER, mint=pk(99)))):
            got = self.check(sol_tx(pre=[tb(1, 9_000_000, SENDER)], post=[tb(1, 0, SENDER), entry]))
            self.assertEqual((got.status, got.reason), ("rejected", reason))
        tx = good_sol_tx()
        tx["slot"] = "1"  # not an integer: unknown, never guessed
        self.assertIsNone(self.check(tx).slot)

    def test_account_exists(self):
        def info(value):
            def reply(params):
                self.assertEqual(params[0], RECV_ATA)
                self.assertEqual(params[1]["commitment"], "confirmed")
                return {"context": {"slot": 1}, "value": value}
            return reply
        self.assertFalse(self.solana(FakeRpc(getAccountInfo=info(None))).account_exists(RECV_ATA))
        self.assertTrue(self.solana(FakeRpc(getAccountInfo=info({"lamports": 2039280, "data": ["", "base64"]})))
                        .account_exists(RECV_ATA))
        for broken in (OSError("down"), None, {"error": {"code": 429, "message": "slow"}}):
            with self.assertRaises(chain.ChainError):
                self.solana(FakeRpc(getAccountInfo=broken)).account_exists(RECV_ATA)

    def test_audit_with_many_token_balances_and_other_mints(self):
        keys = [SENDER, SENDER_ATA, RECV_ATA, USDC_MINT, TOKEN_PROGRAM, OTHER_ATA, pk(78)]
        # The receiver's pre entry is of another mint: filtered out, so before is unknown.
        tx = sol_tx(pre=[tb(1, 9_000_000, SENDER), tb(2, 777, RECV_OWNER, mint=pk(99)),
                         tb(5, 40, OTHER), tb(6, 50, OTHER, mint=pk(77))],
                    post=[tb(6, 10, OTHER, mint=pk(77)), tb(5, 40, OTHER),
                          tb(2, 3_000_000, RECV_OWNER), tb(1, 6_000_000, SENDER)], keys=keys)
        got = self.check(tx)
        self.assertEqual((got.status, got.amount_micro, got.receiver_before_micro,
                          got.receiver_after_micro), ("confirmed", 3_000_000, None, 3_000_000))
        tx = sol_tx(pre=[tb(6, 50, OTHER, mint=pk(77)), tb(5, 40, OTHER), tb(2, 1_000, RECV_OWNER),
                         tb(1, 9_000_000, SENDER)],
                    post=[tb(1, 6_000_000, SENDER), tb(2, 3_001_000, RECV_OWNER), tb(5, 40, OTHER),
                          tb(6, 10, OTHER, mint=pk(77))], keys=keys)
        got = self.check(tx)
        self.assertEqual((got.receiver_before_micro, got.receiver_after_micro), (1_000, 3_001_000))

    def test_unconfirmed_checks_carry_no_audit(self):
        for result in (None, OSError("down"), sol_tx(pre=[tb(1, 9, SENDER), tb(2, 5, RECV_OWNER)],
                                                     post=[tb(1, 9, SENDER), tb(2, 5, RECV_OWNER)])):
            got = self.check(result)
            self.assertNotEqual(got.status, "confirmed")
            self.assertEqual((got.receiver_before_micro, got.receiver_after_micro, got.slot,
                              got.block_time), (None, None, None, None))

    def test_receiver_balance(self):
        def balance(params):
            self.assertEqual(params, [RECV_ATA, {"commitment": "finalized"}])
            return {"context": {"slot": 5}, "value": {"amount": "12345", "decimals": 6}}
        self.assertEqual(self.solana(FakeRpc(getTokenAccountBalance=balance)).receiver_balance(), 12345)
        for broken in ({"value": {}}, {"value": {"amount": 1.5}}, OSError("down"),
                       {"error": {"code": 429, "message": "slow down"}}):
            with self.assertRaises(chain.ChainError):
                self.solana(FakeRpc(getTokenAccountBalance=broken)).receiver_balance()

    def test_token_accounts_and_blockhash(self):
        def accounts(params):
            self.assertEqual(params[0], SENDER)
            self.assertEqual(params[1], {"mint": USDC_MINT})
            self.assertEqual(params[2]["encoding"], "jsonParsed")
            return {"context": {"slot": 1}, "value": [
                {"pubkey": pk(40 + i), "account": {"data": {"parsed": {"info": {
                    "mint": USDC_MINT, "owner": SENDER, "tokenAmount": {"amount": str(a), "decimals": 6}}}}}}
                for i, a in enumerate([5, 700, 0, 31])]}
        bh = pk(50)
        sol = self.solana(FakeRpc(getTokenAccountsByOwner=accounts,
                                  getLatestBlockhash={"context": {"slot": 1},
                                                      "value": {"blockhash": bh, "lastValidBlockHeight": 9}}))
        self.assertEqual(sol.token_accounts(SENDER), [pk(41), pk(43), pk(40), pk(42)])
        self.assertEqual(sol.latest_blockhash(), bh)
        broken = self.solana(FakeRpc(getTokenAccountsByOwner=OSError("down"), getLatestBlockhash={"value": {}}))
        with self.assertRaises(chain.ChainError):
            broken.token_accounts(SENDER)
        with self.assertRaises(chain.ChainError):
            broken.latest_blockhash()


BASE_RECV = "0x" + "ab" * 20
BASE_FROM = "0x" + "12" * 20
BASE_OTHER = "0x" + "34" * 20
TX_HASH = "0x" + "9f" * 32


def topic_addr(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def transfer_log(frm, to, amount, contract=BASE_USDC, removed=False):
    return {"address": contract, "topics": [chain.TRANSFER_TOPIC, topic_addr(frm), topic_addr(to)],
            "data": "0x%064x" % amount, "removed": removed}


def receipt(logs, status="0x1", block=100):
    return {"transactionHash": TX_HASH, "status": status, "blockNumber": hex(block), "logs": logs}


class BaseCheckTest(unittest.TestCase):
    def check(self, rec, latest=200, confirmations=12):
        rpc = FakeRpc(eth_getTransactionReceipt=rec, eth_blockNumber=latest if isinstance(latest, Exception) else hex(latest))
        base = chain.Base("http://base", BASE_RECV.upper().replace("0X", "0x"), BASE_USDC.lower(),
                          confirmations=confirmations, post=rpc)
        return base.check(TX_HASH)

    def test_confirmed(self):
        got = self.check(receipt([transfer_log(BASE_FROM, BASE_RECV, 2_500_000)]))
        self.assertEqual(got, chain.DepositCheck("confirmed", BASE_FROM, 2_500_000, "ok"))
        # Exactly at the threshold: blockNumber + confirmations == latest.
        self.assertEqual(self.check(receipt([transfer_log(BASE_FROM, BASE_RECV, 1)]), latest=112).status, "confirmed")

    def test_sums_same_sender_and_ignores_unrelated_logs(self):
        logs = [
            transfer_log(BASE_FROM, BASE_RECV, 1_000_000),
            transfer_log(BASE_FROM, BASE_OTHER, 7_000_000),  # not to receiver
            transfer_log(BASE_OTHER, BASE_RECV, 9, removed=True),  # reorged log
            {"address": BASE_USDC, "topics": ["0x" + "11" * 32], "data": "0x"},
            transfer_log(BASE_FROM, BASE_RECV, 500_000),
        ]
        self.assertEqual(self.check(receipt(logs)), chain.DepositCheck("confirmed", BASE_FROM, 1_500_000, "ok"))

    def test_pending(self):
        self.assertEqual(self.check(None).status, "pending")
        low = self.check(receipt([transfer_log(BASE_FROM, BASE_RECV, 1)]), latest=111)
        self.assertEqual(low.status, "pending")
        self.assertIn("11/12", low.reason)
        self.assertEqual(self.check(OSError("unreachable")).status, "pending")
        self.assertEqual(self.check(receipt([]), latest=OSError("unreachable")).status, "pending")
        self.assertEqual(self.check({"error": {"code": -32000, "message": "busy"}}).status, "pending")

    def test_rejected(self):
        cases = {
            "transaction_reverted": receipt([transfer_log(BASE_FROM, BASE_RECV, 1)], status="0x0"),
            "wrong_token_contract": receipt([transfer_log(BASE_FROM, BASE_RECV, 5, contract="0x" + "77" * 20)]),
            "no_usdc_transfer_to_receiver": receipt([transfer_log(BASE_FROM, BASE_OTHER, 5)]),
            "mixed_senders": receipt([transfer_log(BASE_FROM, BASE_RECV, 5), transfer_log(BASE_OTHER, BASE_RECV, 5)]),
            "zero_amount": receipt([transfer_log(BASE_FROM, BASE_RECV, 0)]),
        }
        for reason, rec in cases.items():
            got = self.check(rec)
            self.assertEqual((got.status, got.reason, got.amount_micro), ("rejected", reason, 0), reason)
        base = chain.Base("http://base", BASE_RECV, BASE_USDC, post=FakeRpc())
        for bad in ("0x1234", "9f" * 32, "0x" + "zz" * 32, None):
            self.assertEqual(base.check(bad).status, "rejected")


class BaseAuditTest(unittest.TestCase):
    def base(self, **responses):
        rpc = FakeRpc(eth_getTransactionReceipt=receipt([transfer_log(BASE_FROM, BASE_RECV, 2_500_000)]),
                      eth_blockNumber=hex(200), **responses)
        return chain.Base("http://base", BASE_RECV, BASE_USDC, post=rpc), rpc

    def test_balances_at_the_block_before_and_of_the_transfer(self):
        def balance_of(params):
            call, tag = params
            self.assertEqual(call, {"to": BASE_USDC.lower(),
                                    "data": "0x70a08231" + "0" * 24 + BASE_RECV[2:]})
            return {hex(99): "0x%064x" % 7_000_000, hex(100): "0x%064x" % 9_500_000,
                    "latest": "0x%064x" % 9_600_000}[tag]
        base, rpc = self.base(eth_call=balance_of,
                              eth_getBlockByNumber=lambda p: {"number": p[0], "timestamp": hex(1_800_000_000)})
        got = base.check(TX_HASH)
        self.assertEqual((got.status, got.amount_micro, got.receiver_before_micro,
                          got.receiver_after_micro, got.slot, got.block_time),
                         ("confirmed", 2_500_000, 7_000_000, 9_500_000, 100, 1_800_000_000))
        self.assertEqual(base.receiver_balance(), 9_600_000)

    def test_no_historic_state_is_none_never_a_guess(self):
        base, _ = self.base(eth_call={"error": {"code": -32000, "message": "missing trie node"}},
                            eth_getBlockByNumber=OSError("down"))
        got = base.check(TX_HASH)
        self.assertEqual((got.status, got.amount_micro, got.receiver_before_micro,
                          got.receiver_after_micro, got.slot, got.block_time),
                         ("confirmed", 2_500_000, None, None, 100, None))
        base, _ = self.base(eth_call="0xzz", eth_getBlockByNumber={})
        got = base.check(TX_HASH)
        self.assertEqual((got.status, got.receiver_before_micro, got.block_time), ("confirmed", None, None))
        with self.assertRaises(chain.ChainError):
            base.receiver_balance()


if __name__ == "__main__":
    unittest.main()
