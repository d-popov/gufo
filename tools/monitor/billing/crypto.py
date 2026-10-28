"""Pure-Python crypto for the credits portal: base58, Ed25519 verification,
Keccak-256, EIP-191 address recovery and unsigned Solana USDC transfers.

Verification only: no private keys are ever handled here. Python 3.11+
standard library only.
"""

from __future__ import annotations

import hashlib

# ---------------------------------------------------------------- base58

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(b: bytes) -> str:
    """Bitcoin/Solana base58 (no checksum); leading zero bytes become '1'."""
    b = bytes(b)
    zeros = len(b) - len(b.lstrip(b"\0"))
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58_ALPHABET[r])
    return "1" * zeros + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    """Inverse of b58encode; raises ValueError on characters outside the alphabet."""
    if not isinstance(s, str):
        raise ValueError("base58 input must be str")
    n = 0
    for c in s:
        digit = _B58_INDEX.get(c)
        if digit is None:
            raise ValueError(f"invalid base58 character {c!r}")
        n = n * 58 + digit
    zeros = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * zeros + body


# ---------------------------------------------------------------- Ed25519 (RFC 8032)

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _ed_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % _P
    pb = (y1 + x1) * (y2 + x2) % _P
    pc = 2 * t1 * t2 * _D % _P
    pd = 2 * z1 * z2 % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _ed_mul(k: int, point):
    acc = (0, 1, 1, 0)
    while k:
        if k & 1:
            acc = _ed_add(acc, point)
        point = _ed_add(point, point)
        k >>= 1
    return acc


def _ed_equal(a, b) -> bool:
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _ed_recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if x & 1 != sign:
        x = _P - x
    return x


def _ed_decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_G_Y = 4 * pow(5, -1, _P) % _P
_G = (_ed_recover_x(_G_Y, 0), _G_Y, 1, _ed_recover_x(_G_Y, 0) * _G_Y % _P)


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """RFC 8032 Ed25519 verification ([S]B == R + [k]A); rejects S >= L and bad points."""
    try:
        pub, msg, sig = bytes(pub), bytes(msg), bytes(sig)
    except TypeError:
        return False
    if len(pub) != 32 or len(sig) != 64:
        return False
    a = _ed_decompress(pub)
    if a is None:
        return False
    r = _ed_decompress(sig[:32])
    if r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _L
    return _ed_equal(_ed_mul(s, _G), _ed_add(r, _ed_mul(k, a)))


# ---------------------------------------------------------------- Keccak-256

_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
# Rotation offsets indexed [x + 5*y].
_KECCAK_ROT = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)
_M64 = (1 << 64) - 1


def _keccak_f(a: list[int]) -> None:
    for rc in _KECCAK_RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        for x in range(5):
            d = c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64)
            for y in range(0, 25, 5):
                a[x + y] ^= d
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                v = a[x + 5 * y]
                r = _KECCAK_ROT[x + 5 * y]
                b[y + 5 * ((2 * x + 3 * y) % 5)] = ((v << r) | (v >> (64 - r))) & _M64 if r else v
        for y in range(0, 25, 5):
            row = b[y:y + 5]
            for x in range(5):
                a[x + y] = row[x] ^ (~row[(x + 1) % 5] & row[(x + 2) % 5])
        a[0] ^= rc


def keccak256(b: bytes) -> bytes:
    """Original Keccak-256 (Ethereum), padding 0x01..0x80, not NIST SHA3-256."""
    rate = 136
    data = bytearray(b)
    pad = rate - len(data) % rate
    data += b"\0" * pad
    data[len(b)] ^= 0x01
    data[-1] ^= 0x80
    state = [0] * 25
    for off in range(0, len(data), rate):
        block = data[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


# ---------------------------------------------------------------- secp256k1 / EIP-191

_K_P = 2**256 - 2**32 - 977
_K_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_K_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _k_add(a, b):
    # Jacobian coordinates; None is the point at infinity.
    if a is None:
        return b
    if b is None:
        return a
    x1, y1, z1 = a
    x2, y2, z2 = b
    z1z1, z2z2 = z1 * z1 % _K_P, z2 * z2 % _K_P
    u1, u2 = x1 * z2z2 % _K_P, x2 * z1z1 % _K_P
    s1, s2 = y1 * z2 * z2z2 % _K_P, y2 * z1 * z1z1 % _K_P
    if u1 == u2:
        return _k_double(a) if s1 == s2 else None
    h = (u2 - u1) % _K_P
    r = (s2 - s1) % _K_P
    hh = h * h % _K_P
    hhh = h * hh % _K_P
    v = u1 * hh % _K_P
    x3 = (r * r - hhh - 2 * v) % _K_P
    y3 = (r * (v - x3) - s1 * hhh) % _K_P
    return (x3, y3, h * z1 * z2 % _K_P)


def _k_double(a):
    if a is None:
        return None
    x, y, z = a
    if y == 0:
        return None
    yy = y * y % _K_P
    s = 4 * x * yy % _K_P
    m = 3 * x * x % _K_P
    x3 = (m * m - 2 * s) % _K_P
    y3 = (m * (s - x3) - 8 * yy * yy) % _K_P
    return (x3, y3, 2 * y * z % _K_P)


def _k_mul(k: int, point):
    acc = None
    while k:
        if k & 1:
            acc = _k_add(acc, point)
        point = _k_double(point)
        k >>= 1
    return acc


def _k_affine(a):
    if a is None:
        return None
    x, y, z = a
    zi = pow(z, -1, _K_P)
    zi2 = zi * zi % _K_P
    return (x * zi2 % _K_P, y * zi2 * zi % _K_P)


def eth_address_from_pubkey(x: int, y: int) -> str:
    """Lowercase 0x address of an uncompressed secp256k1 public key."""
    return "0x" + keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:].hex()


def eth_recover_personal(message: str, sig_hex: str) -> str | None:
    """Signer address (lowercase 0x) of an EIP-191 personal_sign signature, or None."""
    try:
        raw = bytes.fromhex(sig_hex[2:] if sig_hex[:2] in ("0x", "0X") else sig_hex)
        data = message.encode("utf-8")
    except (ValueError, AttributeError, TypeError):
        return None
    if len(raw) != 65:
        return None
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    v = raw[64]
    if v >= 27:
        v -= 27
    if v not in (0, 1) or not (0 < r < _K_N) or not (0 < s < _K_N):
        return None
    digest = keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    z = int.from_bytes(digest, "big")
    # r < n < p, so x = r always fits; the x = r + n branch needs v in {2, 3}.
    alpha = (pow(r, 3, _K_P) + 7) % _K_P
    y = pow(alpha, (_K_P + 1) // 4, _K_P)
    if y * y % _K_P != alpha:
        return None
    if y & 1 != v:
        y = _K_P - y
    r_inv = pow(r, -1, _K_N)
    q = _k_add(_k_mul(s * r_inv % _K_N, (r, y, 1)), _k_mul((-z * r_inv) % _K_N, (_K_G[0], _K_G[1], 1)))
    q = _k_affine(q)
    if q is None:
        return None
    return eth_address_from_pubkey(*q)


# ---------------------------------------------------------------- Solana transaction

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TRANSFER_CHECKED = 12


def _compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pubkey(s: str, what: str) -> bytes:
    try:
        raw = b58decode(s)
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from None
    if len(raw) != 32:
        raise ValueError(f"{what}: expected 32-byte public key")
    return raw


def solana_transfer_checked_tx(owner: str, source_token_account: str, mint: str,
                               dest_token_account: str, amount: int, decimals: int,
                               recent_blockhash: str) -> bytes:
    """Unsigned legacy transaction (one zeroed signature slot) moving `amount`
    base units of `mint` from the owner's token account with SPL Token
    TransferChecked; the owner is fee payer and sole signer."""
    if not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount < 2**64:
        raise ValueError("amount must be a positive u64")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals < 256:
        raise ValueError("decimals must be a u8")
    # Order: writable signer, writable non-signers, read-only non-signers.
    keys = [
        _pubkey(owner, "owner"),
        _pubkey(source_token_account, "source_token_account"),
        _pubkey(dest_token_account, "dest_token_account"),
        _pubkey(mint, "mint"),
        _pubkey(TOKEN_PROGRAM_ID, "token program"),
    ]
    if len(set(keys)) != len(keys):
        raise ValueError("owner, source, destination and mint must be distinct accounts")
    blockhash = _pubkey(recent_blockhash, "recent_blockhash")
    owner_i, source_i, dest_i, mint_i, program_i = range(5)
    data = bytes([_TRANSFER_CHECKED]) + amount.to_bytes(8, "little") + bytes([decimals])
    accounts = bytes([source_i, mint_i, dest_i, owner_i])
    message = (
        bytes([1, 0, 2])  # required signatures, read-only signed, read-only unsigned
        + _compact_u16(len(keys)) + b"".join(keys)
        + blockhash
        + _compact_u16(1)
        + bytes([program_i]) + _compact_u16(len(accounts)) + accounts
        + _compact_u16(len(data)) + data
    )
    return _compact_u16(1) + b"\0" * 64 + message
