"""Password hashing and API-token digests using only the standard library.

Deliberately no external crypto dependency: ``hashlib.scrypt`` is a memory-hard KDF in the
standard library, and tokens are compared with :func:`hmac.compare_digest`.

Formats are self-describing so parameters can be rotated without a flag day:

* password: ``scrypt$n=<int>,r=<int>,p=<int>$<salt-b64>$<hash-b64>``
* token digest: ``sha256$<hex>`` of ``token_id.secret`` (the secret is never stored).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

__all__ = [
    "TOKEN_PREFIX_LENGTH",
    "constant_time_equals",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "new_api_token",
    "verify_password",
    "verify_token",
]

# scrypt parameters: 2**14 iterations, 8 blocks, 1 parallelisation → ~16 MiB per hash.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_DK_LEN = 32
TOKEN_PREFIX_LENGTH = 12


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DK_LEN)
    return f"scrypt$n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, parameters, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        params = dict(item.split("=", 1) for item in parameters.split(","))
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_b64),
            n=int(params["n"]),
            r=int(params["r"]),
            p=int(params["p"]),
            dklen=len(_unb64(hash_b64)),
        )
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(hash_b64))


def needs_rehash(encoded: str) -> bool:
    """True when the stored parameters are weaker than the current policy."""
    try:
        scheme, parameters, _, _ = encoded.split("$")
        if scheme != "scrypt":
            return True
        params = dict(item.split("=", 1) for item in parameters.split(","))
        return int(params["n"]) < _SCRYPT_N
    except (ValueError, KeyError):
        return True


def new_api_token(*, prefix: str = "dk") -> tuple[str, str, str]:
    """Mint an API token.

    Returns ``(token, token_prefix, token_hash)``. The clear token is shown to the user exactly
    once; only ``token_hash`` is stored. The prefix identifies the token row for lookup, so
    verification is a single indexed read plus one constant-time comparison.
    """
    token_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:TOKEN_PREFIX_LENGTH]
    secret = secrets.token_urlsafe(32)
    token = f"{prefix}_{token_id}_{secret}"
    return token, token_id, hash_token(token_id, secret)


def hash_token(token_id: str, secret: str) -> str:
    return "sha256$" + hashlib.sha256(f"{token_id}.{secret}".encode()).hexdigest()


def verify_token(token: str, token_prefix: str, expected_hash: str) -> bool:
    try:
        _prefix, token_id, secret = token.split("_", 2)
    except ValueError:
        return False
    if token_id != token_prefix:
        return False
    return constant_time_equals(hash_token(token_id, secret), expected_hash)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
