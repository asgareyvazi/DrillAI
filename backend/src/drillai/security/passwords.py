"""Hardened password hashing (`scrypt` OWASP `N=2^15` + `Argon2id`) and API-token digests.

Supports two self-describing memory-hard KDF formats so parameters or algorithms can be
rotated transparently on login without a flag day:

* **Hardened scrypt (default)**: ``scrypt$n=32768,r=8,p=1$<salt-b64>$<hash-b64>`` (~32 MiB per hash,
  configurable up to ``n=65536`` / ~64 MiB; backward-compatible with legacy ``n=16384`` hashes,
  which trigger ``needs_rehash(...) is True`` for automatic upgrade on login).
* **Argon2id (OWASP minimum `m=19456` KiB, `t=2`, `p=1`)**:
  ``$argon2id$v=19$m=19456,t=2,p=1$<salt-b64>$<hash-b64>`` via ``cryptography`` OpenSSL KDF.
* **API token digest**: ``sha256$<hex>`` of ``token_id.secret`` (the raw secret is never stored).

DoS & Concurrency Defenses
--------------------------
1. **Parameter Bounds Enforcement**: :func:`verify_password` validates ``n`` (power of two in
   ``[1024, 65536]``), ``r`` (``[1, 16]``), ``p`` (``[1, 4]``), total scrypt memory ``128*r*n*p <= 64 MiB``,
   and Argon2id ``m`` (``[8192, 65536]`` KiB), ``t`` (``[1, 8]``), ``p`` (``[1, 4]``) *before*
   invoking any KDF computation, neutralizing CPU/memory DoS via crafted hash strings.
2. **Bounded Concurrency**: :func:`hash_password_async` and :func:`verify_password_async` acquire a
   process-wide semaphore and execute on a dedicated bounded KDF thread pool so concurrent login
   bursts cannot exhaust process memory or block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import secrets
import statistics
import threading
import time
from typing import Any, Literal

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

__all__ = [
    "ARGON2ID_DEFAULT_LANES",
    "ARGON2ID_DEFAULT_MEMORY_KIB",
    "ARGON2ID_DEFAULT_TIME_COST",
    "SCRYPT_DEFAULT_N",
    "SCRYPT_DEFAULT_P",
    "SCRYPT_DEFAULT_R",
    "TOKEN_PREFIX_LENGTH",
    "benchmark_password_kdf",
    "constant_time_equals",
    "hash_password",
    "hash_password_async",
    "hash_token",
    "needs_rehash",
    "new_api_token",
    "verify_password",
    "verify_password_async",
    "verify_token",
]

# OWASP-compliant scrypt parameters: 2**15 (32,768) iterations, r=8, p=1 → ~32 MiB per hash.
SCRYPT_DEFAULT_N = 2**15
SCRYPT_DEFAULT_R = 8
SCRYPT_DEFAULT_P = 1
_SCRYPT_MIN_VERIFY_N = 2**10  # 1,024 minimum accepted for verification
_SCRYPT_MAX_VERIFY_N = 2**16  # 65,536 maximum accepted (~64 MiB at r=8, p=1)
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 4
_SCRYPT_MAXMEM_BYTES = 64 * 1024 * 1024  # 64 MiB hard cap per KDF invocation
_SCRYPT_OPENSSL_MAXMEM = 128 * 1024 * 1024

# OWASP-compliant Argon2id parameters: m=19456 KiB (~19 MiB), t=2 iterations, p=1 lane.
ARGON2ID_DEFAULT_MEMORY_KIB = 19456
ARGON2ID_DEFAULT_TIME_COST = 2
ARGON2ID_DEFAULT_LANES = 1
_ARGON2ID_MIN_VERIFY_MEMORY_KIB = 8192
_ARGON2ID_MAX_VERIFY_MEMORY_KIB = 65536
_ARGON2ID_MAX_VERIFY_TIME_COST = 8
_ARGON2ID_MAX_VERIFY_LANES = 4

_SALT_BYTES = 16
_DK_LEN = 32
_MAX_PASSWORD_BYTES = 1024
TOKEN_PREFIX_LENGTH = 12

_KDF_MAX_CONCURRENCY = 4
_KDF_SEMAPHORE = threading.BoundedSemaphore(_KDF_MAX_CONCURRENCY)
_KDF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_KDF_MAX_CONCURRENCY,
    thread_name_prefix="drillai-kdf",
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _is_power_of_two(val: int) -> bool:
    return val > 1 and (val & (val - 1)) == 0


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    scheme: Literal["scrypt", "argon2id"] = "scrypt",
    scrypt_n: int = SCRYPT_DEFAULT_N,
) -> str:
    """Hash ``password`` using hardened ``scrypt`` (default, `N=2^15`) or ``argon2id`` (`m=19456 KiB`)."""
    if not password:
        raise ValueError("password must not be empty")
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        raise ValueError("password exceeds maximum byte length")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    if len(salt) < 8 or len(salt) > 64:
        raise ValueError("salt must be between 8 and 64 bytes")

    if scheme == "argon2id":
        with _KDF_SEMAPHORE:
            kdf = Argon2id(
                salt=salt,
                length=_DK_LEN,
                iterations=ARGON2ID_DEFAULT_TIME_COST,
                lanes=ARGON2ID_DEFAULT_LANES,
                memory_cost=ARGON2ID_DEFAULT_MEMORY_KIB,
            )
            return kdf.derive_phc_encoded(pw_bytes)

    if scheme != "scrypt":
        raise ValueError(f"unsupported password hash scheme: {scheme!r}")
    if not _is_power_of_two(scrypt_n) or scrypt_n < 2**14 or scrypt_n > _SCRYPT_MAX_VERIFY_N:
        raise ValueError(f"scrypt_n must be a power of two in [16384, {_SCRYPT_MAX_VERIFY_N}]")

    with _KDF_SEMAPHORE:
        digest = hashlib.scrypt(
            pw_bytes,
            salt=salt,
            n=scrypt_n,
            r=SCRYPT_DEFAULT_R,
            p=SCRYPT_DEFAULT_P,
            maxmem=_SCRYPT_OPENSSL_MAXMEM,
            dklen=_DK_LEN,
        )
    return f"scrypt$n={scrypt_n},r={SCRYPT_DEFAULT_R},p={SCRYPT_DEFAULT_P}${_b64(salt)}${_b64(digest)}"


def _verify_argon2id(password_bytes: bytes, encoded: str) -> bool:
    """Validate Argon2id PHC parameters before delegating to OpenSSL `Argon2id.verify_phc_encoded`."""
    # Format: $argon2id$v=19$m=<int>,t=<int>,p=<int>$<salt_b64>$<hash_b64>
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "" or parts[1] != "argon2id" or parts[2] != "v=19":
        return False
    try:
        params = dict(item.split("=", 1) for item in parts[3].split(","))
        m = int(params["m"])
        t = int(params["t"])
        p = int(params["p"])
    except (ValueError, KeyError, TypeError):
        return False
    if not (_ARGON2ID_MIN_VERIFY_MEMORY_KIB <= m <= _ARGON2ID_MAX_VERIFY_MEMORY_KIB):
        return False
    if not (1 <= t <= _ARGON2ID_MAX_VERIFY_TIME_COST):
        return False
    if not (1 <= p <= _ARGON2ID_MAX_VERIFY_LANES):
        return False
    if not parts[4] or not parts[5] or len(parts[4]) > 128 or len(parts[5]) > 128:
        return False
    try:
        with _KDF_SEMAPHORE:
            Argon2id.verify_phc_encoded(password_bytes, encoded)
        return True
    except (InvalidKey, ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    """Verify ``password`` against a ``scrypt$...`` or ``$argon2id$...`` hash with strict parameter bounds."""
    if not isinstance(password, str) or not isinstance(encoded, str) or not password or not encoded:
        return False
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        return False

    if encoded.startswith("$argon2id$"):
        return _verify_argon2id(pw_bytes, encoded)

    try:
        parts = encoded.split("$")
        if len(parts) != 4:
            return False
        scheme, parameters, salt_b64, hash_b64 = parts
        if scheme != "scrypt":
            return False
        params = dict(item.split("=", 1) for item in parameters.split(","))
        if set(params.keys()) != {"n", "r", "p"}:
            return False
        n = int(params["n"])
        r = int(params["r"])
        p = int(params["p"])
        # Reject malformed or extreme DoS parameters BEFORE invoking hashlib.scrypt
        if not _is_power_of_two(n) or not (_SCRYPT_MIN_VERIFY_N <= n <= _SCRYPT_MAX_VERIFY_N):
            return False
        if not (1 <= r <= _SCRYPT_MAX_R) or not (1 <= p <= _SCRYPT_MAX_P):
            return False
        estimated_mem = 128 * r * n * p
        if estimated_mem > _SCRYPT_MAXMEM_BYTES:
            return False
        salt = _unb64(salt_b64)
        expected_digest = _unb64(hash_b64)
        if not (8 <= len(salt) <= 64) or not (16 <= len(expected_digest) <= 64):
            return False
        with _KDF_SEMAPHORE:
            digest = hashlib.scrypt(
                pw_bytes,
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=_SCRYPT_OPENSSL_MAXMEM,
                dklen=len(expected_digest),
            )
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_digest)


async def hash_password_async(
    password: str,
    *,
    scheme: Literal["scrypt", "argon2id"] = "scrypt",
) -> str:
    """Hash ``password`` on the bounded KDF thread pool without blocking the asyncio event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _KDF_EXECUTOR,
        lambda: hash_password(password, scheme=scheme),
    )


async def verify_password_async(password: str, encoded: str) -> bool:
    """Verify ``password`` on the bounded KDF thread pool without blocking the asyncio event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _KDF_EXECUTOR,
        verify_password,
        password,
        encoded,
    )


def needs_rehash(encoded: str) -> bool:
    """Return ``True`` when the stored hash uses weaker parameters than the current OWASP policy."""
    if not isinstance(encoded, str) or not encoded:
        return True
    if encoded.startswith("$argon2id$"):
        parts = encoded.split("$")
        if len(parts) != 6 or parts[1] != "argon2id" or parts[2] != "v=19":
            return True
        try:
            params = dict(item.split("=", 1) for item in parts[3].split(","))
            m = int(params["m"])
            t = int(params["t"])
            p = int(params["p"])
            return (
                m < ARGON2ID_DEFAULT_MEMORY_KIB
                or t < ARGON2ID_DEFAULT_TIME_COST
                or p < ARGON2ID_DEFAULT_LANES
            )
        except (ValueError, KeyError, TypeError):
            return True

    try:
        scheme, parameters, _, _ = encoded.split("$")
        if scheme != "scrypt":
            return True
        params = dict(item.split("=", 1) for item in parameters.split(","))
        n = int(params["n"])
        r = int(params["r"])
        p = int(params["p"])
        if not _is_power_of_two(n):
            return True
        return n < SCRYPT_DEFAULT_N or r < SCRYPT_DEFAULT_R or p < SCRYPT_DEFAULT_P
    except (ValueError, KeyError, TypeError):
        return True


def benchmark_password_kdf(*, samples: int = 7) -> dict[str, Any]:
    """Measure p50/p95 verification latency and memory footprint for `scrypt` (`N=2^15`) and `argon2id`."""
    count = max(3, min(samples, 25))
    probe_pw = "Benchmark-DrillAI-KDF-Pass!2026"

    scrypt_hash = hash_password(probe_pw, scheme="scrypt")
    scrypt_latencies_ms: list[float] = []
    for _ in range(count):
        t0 = time.perf_counter()
        assert verify_password(probe_pw, scrypt_hash) is True
        scrypt_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    scrypt_sorted = sorted(scrypt_latencies_ms)
    scrypt_p95_idx = min(len(scrypt_sorted) - 1, round(0.95 * (len(scrypt_sorted) - 1)))

    argon_hash = hash_password(probe_pw, scheme="argon2id")
    argon_latencies_ms: list[float] = []
    for _ in range(count):
        t0 = time.perf_counter()
        assert verify_password(probe_pw, argon_hash) is True
        argon_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    argon_sorted = sorted(argon_latencies_ms)
    argon_p95_idx = min(len(argon_sorted) - 1, round(0.95 * (len(argon_sorted) - 1)))

    scrypt_mem_mib = (128 * SCRYPT_DEFAULT_R * SCRYPT_DEFAULT_N * SCRYPT_DEFAULT_P) / (1024 * 1024)
    argon_mem_mib = ARGON2ID_DEFAULT_MEMORY_KIB / 1024.0
    return {
        "samples": count,
        "max_concurrency": _KDF_MAX_CONCURRENCY,
        "scrypt": {
            "n": SCRYPT_DEFAULT_N,
            "r": SCRYPT_DEFAULT_R,
            "p": SCRYPT_DEFAULT_P,
            "memory_mib": round(scrypt_mem_mib, 2),
            "p50_ms": round(statistics.median(scrypt_sorted), 2),
            "p95_ms": round(scrypt_sorted[scrypt_p95_idx], 2),
        },
        "argon2id": {
            "memory_kib": ARGON2ID_DEFAULT_MEMORY_KIB,
            "memory_mib": round(argon_mem_mib, 2),
            "time_cost": ARGON2ID_DEFAULT_TIME_COST,
            "lanes": ARGON2ID_DEFAULT_LANES,
            "p50_ms": round(statistics.median(argon_sorted), 2),
            "p95_ms": round(argon_sorted[argon_p95_idx], 2),
        },
    }


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
