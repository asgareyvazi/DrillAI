"""Authenticated encryption and lifecycle helpers for tenant-scoped :class:`SecretRef` rows.

Implements **CP22-A Option 1**: real authenticated encryption at rest (`AES-256-GCM`) for
``SecretRef(backend="db_encrypted")`` rows, keyed by an external master key
(``DRILLAI_SECRET_MASTER_KEY``) that is never stored in the database.

Cryptographic Design
--------------------
* **Algorithm**: ``AES-256-GCM`` (NIST SP 800-38D) via ``cryptography.hazmat.primitives.ciphers.aead.AESGCM``.
* **Key Derivation**: 256-bit per-version subkeys derived via ``HKDF-SHA256`` (RFC 5869) from the
  configured master key for ``key_version`` with domain separation info
  ``b"drillai-secret-ref-aead-v1:kv=<version>"``.
* **Nonce**: 96-bit (12-byte) CSPRNG nonce generated via :func:`secrets.token_bytes` per encryption.
* **Associated Authenticated Data (AAD)**: ``f"org:{org_id}|key:{secret_key}|kv:{key_version}"``.
  Binding ``org_id`` and ``secret_key`` into the GCM authentication tag prevents cross-tenant or
  cross-key ciphertext transplantation even by an attacker with raw SQL write access.
* **Envelope Format**: ``enc:v1:kv=<int>:<nonce_b64>:<ciphertext_and_tag_b64>`` stored in
  ``SecretRef.locator``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from drillai.core.config import Settings, get_settings
from drillai.core.errors import ConfigurationError, ValidationFailed

__all__ = [
    "ENVELOPE_PREFIX",
    "SUPPORTED_SECRET_BACKENDS",
    "decrypt_secret_value",
    "encrypt_secret_value",
    "extract_envelope_key_version",
    "mask_secret_ref_row",
    "needs_key_rotation",
    "rotate_secret_envelope",
]

ENVELOPE_PREFIX = "enc:v1:"
SUPPORTED_SECRET_BACKENDS = ("env", "db_encrypted")
_NONCE_BYTES = 12
_MIN_MASTER_KEY_LENGTH = 16
_MIN_PROD_MASTER_KEY_LENGTH = 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _hkdf_sha256(ikm: bytes, *, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256 extract-and-expand producing a ``length``-byte key."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    prev = b""
    counter = 1
    while len(okm) < length:
        prev = hmac.new(prk, prev + info + bytes([counter]), hashlib.sha256).digest()
        okm += prev
        counter += 1
    return okm[:length]


def _load_master_keyring(settings: Settings | None = None) -> tuple[int, dict[int, bytes]]:
    """Build ``(active_version, {version: derived_aes256_key})`` from runtime configuration.

    Fails closed with :class:`ConfigurationError` when ``DRILLAI_SECRET_MASTER_KEY`` is missing
    or shorter than the minimum required length.
    """
    cfg = settings or get_settings()
    raw_master = (cfg.secret_master_key or "").strip()
    min_len = _MIN_PROD_MASTER_KEY_LENGTH if cfg.is_production else _MIN_MASTER_KEY_LENGTH
    if not raw_master:
        raise ConfigurationError(
            "DRILLAI_SECRET_MASTER_KEY is not configured; cannot encrypt or decrypt database secrets",
            details={"reason": "master_key_missing"},
        )
    if len(raw_master) < min_len:
        raise ConfigurationError(
            f"DRILLAI_SECRET_MASTER_KEY must be at least {min_len} characters long",
            details={"reason": "master_key_too_short", "min_length": min_len},
        )
    active_kv = int(cfg.secret_active_key_version)
    if active_kv < 1:
        raise ConfigurationError(
            "DRILLAI_SECRET_ACTIVE_KEY_VERSION must be >= 1",
            details={"reason": "invalid_key_version"},
        )

    salt = b"drillai.secret_ref.hkdf.salt.v1"
    keyring: dict[int, bytes] = {
        active_kv: _hkdf_sha256(
            raw_master.encode("utf-8"),
            salt=salt,
            info=f"drillai-secret-ref-aead-v1:kv={active_kv}".encode("ascii"),
            length=32,
        )
    }

    prev_raw = (cfg.secret_previous_master_keys or "").strip()
    if prev_raw:
        for entry in prev_raw.split(","):
            item = entry.strip()
            if not item:
                continue
            if ":" not in item:
                raise ConfigurationError(
                    "DRILLAI_SECRET_PREVIOUS_MASTER_KEYS entries must have format 'v<int>:<key>'",
                    details={"reason": "invalid_previous_key_format"},
                )
            ver_part, key_part = item.split(":", 1)
            ver_str = ver_part.strip().lower().removeprefix("v")
            if not ver_str.isdigit() or int(ver_str) < 1:
                raise ConfigurationError(
                    "DRILLAI_SECRET_PREVIOUS_MASTER_KEYS version must be a positive integer",
                    details={"reason": "invalid_previous_key_version"},
                )
            kv = int(ver_str)
            clean_key = key_part.strip()
            if len(clean_key) < _MIN_MASTER_KEY_LENGTH:
                raise ConfigurationError(
                    f"previous master key v{kv} must be at least {_MIN_MASTER_KEY_LENGTH} characters long",
                    details={"reason": "previous_master_key_too_short", "key_version": kv},
                )
            keyring[kv] = _hkdf_sha256(
                clean_key.encode("utf-8"),
                salt=salt,
                info=f"drillai-secret-ref-aead-v1:kv={kv}".encode("ascii"),
                length=32,
            )

    return active_kv, keyring


def _aad_bytes(*, org_id: str, secret_key: str, key_version: int) -> bytes:
    return f"org:{org_id}|key:{secret_key}|kv:{key_version}".encode()


def encrypt_secret_value(
    plaintext: str,
    *,
    org_id: str,
    secret_key: str,
    settings: Settings | None = None,
) -> tuple[str, int]:
    """Encrypt ``plaintext`` using ``AES-256-GCM`` bound to ``(org_id, secret_key, active_version)``.

    Returns ``(envelope, key_version)``.
    """
    if not plaintext:
        raise ValidationFailed(
            "secret value must not be empty",
            details={"field": "secret_value", "reason": "empty_secret"},
        )
    if not org_id or not secret_key:
        raise ValidationFailed(
            "org_id and secret_key are required for AEAD binding",
            details={"reason": "missing_aead_context"},
        )
    active_kv, keyring = _load_master_keyring(settings)
    aes_key = keyring[active_kv]
    nonce = secrets.token_bytes(_NONCE_BYTES)
    aad = _aad_bytes(org_id=org_id, secret_key=secret_key, key_version=active_kv)
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    envelope = f"{ENVELOPE_PREFIX}kv={active_kv}:{_b64(nonce)}:{_b64(ciphertext)}"
    return envelope, active_kv


def extract_envelope_key_version(envelope: str) -> int:
    """Parse ``kv=<int>`` from an ``enc:v1:kv=<int>:<nonce>:<ct>`` envelope."""
    if not isinstance(envelope, str) or not envelope.startswith(ENVELOPE_PREFIX):
        raise ValidationFailed(
            "stored secret envelope is malformed",
            details={"reason": "malformed_secret_envelope"},
        )
    body = envelope.removeprefix(ENVELOPE_PREFIX)
    parts = body.split(":")
    if len(parts) != 3 or not parts[0].startswith("kv="):
        raise ValidationFailed(
            "stored secret envelope is malformed",
            details={"reason": "malformed_secret_envelope"},
        )
    kv_str = parts[0].removeprefix("kv=")
    if not kv_str.isdigit() or int(kv_str) < 1:
        raise ValidationFailed(
            "stored secret envelope has an invalid key version",
            details={"reason": "invalid_envelope_key_version"},
        )
    return int(kv_str)


def decrypt_secret_value(
    envelope: str,
    *,
    org_id: str,
    secret_key: str,
    settings: Settings | None = None,
) -> str:
    """Decrypt and authenticate an ``enc:v1:kv=<int>:<nonce>:<ct>`` envelope.

    Fails closed if the master key is missing, the key version is not present in the keyring,
    the ciphertext/tag was modified, or the ``(org_id, secret_key)`` AAD does not match.
    """
    kv = extract_envelope_key_version(envelope)
    body = envelope.removeprefix(ENVELOPE_PREFIX)
    _, nonce_b64, ct_b64 = body.split(":", 2)
    try:
        nonce = _unb64(nonce_b64)
        ciphertext = _unb64(ct_b64)
    except Exception as exc:
        raise ValidationFailed(
            "stored secret envelope encoding is invalid",
            details={"reason": "malformed_secret_envelope"},
        ) from exc
    if len(nonce) != _NONCE_BYTES or len(ciphertext) <= 16:
        raise ValidationFailed(
            "stored secret envelope nonce or tag length is invalid",
            details={"reason": "malformed_secret_envelope"},
        )

    try:
        _, keyring = _load_master_keyring(settings)
    except ConfigurationError as exc:
        raise ValidationFailed(
            "cannot decrypt database secret: master key is not configured or invalid",
            details={"reason": "master_key_unavailable"},
        ) from exc

    aes_key = keyring.get(kv)
    if aes_key is None:
        raise ValidationFailed(
            f"cannot decrypt database secret: key version v{kv} is not available in keyring",
            details={"reason": "unknown_key_version", "key_version": kv},
        )

    aad = _aad_bytes(org_id=org_id, secret_key=secret_key, key_version=kv)
    try:
        plaintext_bytes = AESGCM(aes_key).decrypt(nonce, ciphertext, aad)
        return plaintext_bytes.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise ValidationFailed(
            "database secret authentication tag verification failed",
            details={"reason": "secret_decryption_failed", "key_version": kv},
        ) from exc


def needs_key_rotation(envelope: str, *, settings: Settings | None = None) -> bool:
    """Return ``True`` if ``envelope`` was encrypted under an older master key version."""
    cfg = settings or get_settings()
    kv = extract_envelope_key_version(envelope)
    return kv != int(cfg.secret_active_key_version)


def rotate_secret_envelope(
    envelope: str,
    *,
    org_id: str,
    secret_key: str,
    new_plaintext: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, int, int]:
    """Decrypt ``envelope`` (unless ``new_plaintext`` is supplied) and re-encrypt under the active key version.

    Returns ``(new_envelope, previous_key_version, new_key_version)``.
    """
    prev_kv = extract_envelope_key_version(envelope) if envelope.startswith(ENVELOPE_PREFIX) else 0
    plaintext = (
        new_plaintext
        if new_plaintext is not None
        else decrypt_secret_value(envelope, org_id=org_id, secret_key=secret_key, settings=settings)
    )
    new_envelope, new_kv = encrypt_secret_value(
        plaintext,
        org_id=org_id,
        secret_key=secret_key,
        settings=settings,
    )
    return new_envelope, prev_kv, new_kv


def mask_secret_ref_row(row: Any, *, settings: Settings | None = None) -> dict[str, Any]:
    """Return a public, redacted summary of a :class:`SecretRef` row (never exposes locator or plaintext)."""
    key_version: int | None = None
    rotation_required = False
    if getattr(row, "backend", None) == "db_encrypted" and getattr(row, "is_active", False):
        try:
            key_version = extract_envelope_key_version(row.locator)
            rotation_required = needs_key_rotation(row.locator, settings=settings)
        except ValidationFailed:
            key_version = None
            rotation_required = True
    return {
        "id": row.id,
        "org_id": row.org_id,
        "key": row.key,
        "backend": row.backend,
        "key_version": key_version,
        "needs_key_rotation": rotation_required,
        "description": row.description,
        "scope": row.scope,
        "is_active": bool(row.is_active),
        "created_by": row.created_by,
        "last_rotated_at": row.last_rotated_at,
        "last_used_at": row.last_used_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "masked_preview": "********",
    }
