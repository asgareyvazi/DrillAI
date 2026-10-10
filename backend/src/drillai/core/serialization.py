"""Canonical JSON + hashing helpers.

Deterministic serialization is required for:

* content-addressed storage (blob sha256), 
* engine provenance (``input_hash``/``output_hash`` so a calculation can be replayed
  and compared later — a hard requirement for defensible engineering results),
* evidence fingerprints (deduplicating extracted records across re-ingestion),
* cache keys.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "value"):  # Enum
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__} to canonical JSON")


def canonical_json(value: Any) -> str:
    """Serialize to JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_default, ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def content_hash(value: Any) -> str:
    """Stable hash of any JSON-serializable structure."""
    return sha256_hex(canonical_json(value))


def short_hash(value: Any, length: int = 12) -> str:
    return content_hash(value)[:length]


def fingerprint(*parts: Any) -> str:
    """Fingerprint of ordered parts (used for evidence dedup)."""
    return sha256_hex("\x1f".join(canonical_json(part) for part in parts))


def rounded(value: float, digits: int = 6) -> float:
    """Round for hashing/display while avoiding float noise (0.1+0.2 style)."""
    return float(f"{value:.{digits}g}")
