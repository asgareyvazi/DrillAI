"""Stable, sortable identifiers.

The platform uses ULID-style identifiers (48-bit millisecond timestamp + 80 bits of
randomness, Crockford base32) behind a human-readable type prefix, e.g.
``well_01J8Z0Q3M5N7P9R1S3T5V7X9Z1``.

Why ULIDs instead of UUIDv4:
* lexicographic ordering == chronological ordering, which matters for the append-only
  engineering history (events, operations, evidence) where "what happened first" is a
  first-class question;
* prefix keeps ids self-describing in logs, traces, URLs and cross-system payloads
  (Anthropic/OpenAI tool calls, WITSML correlation ids, webhook callbacks);
* still globally unique and collision-safe for distributed writers.

The timestamp and randomness sources are injectable so that tests are deterministic.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_INDEX = {c: i for i, c in enumerate(_CROCKFORD)}
_ULID_LENGTH = 26  # 10 chars timestamp (48 bits) + 16 chars randomness (80 bits)

_TIME_SOURCE: Callable[[], float] = time.time
_RANDOM_SOURCE: Callable[[int], bytes] = os.urandom


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Return a 26 character ULID string."""
    if timestamp_ms is None:
        timestamp_ms = int(_TIME_SOURCE() * 1000)
    if not 0 <= timestamp_ms < (1 << 48):
        raise ValueError("timestamp_ms out of ULID range")
    randomness = _RANDOM_SOURCE(10)
    if len(randomness) != 10:
        raise ValueError("randomness source must return 10 bytes")
    return _encode(timestamp_ms, 10) + _encode(int.from_bytes(randomness, "big"), 16)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def decode_ulid_timestamp_ms(ulid: str) -> int:
    """Extract the millisecond timestamp encoded in a ULID."""
    core = ulid.rsplit("_", 1)[-1]
    if len(core) != _ULID_LENGTH:
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for char in core[:10]:
        try:
            value = (value << 5) | _CROCKFORD_INDEX[char]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid ULID character {char!r}") from exc
    return value


def new_id(prefix: str, *, timestamp_ms: int | None = None) -> str:
    """Return a prefixed identifier such as ``doc_01J8Z...``."""
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{new_ulid(timestamp_ms=timestamp_ms)}"


def is_id(value: str, prefix: str | None = None) -> bool:
    if not isinstance(value, str) or "_" not in value:
        return False
    head, _, core = value.rpartition("_")
    if prefix is not None and head != prefix:
        return False
    if len(core) != _ULID_LENGTH:
        return False
    return all(char in _CROCKFORD_INDEX for char in core)


def set_id_sources(
    *,
    time_source: Callable[[], float] | None = None,
    random_source: Callable[[int], bytes] | None = None,
) -> None:
    """Override id sources. Intended for tests and deterministic replay."""
    global _TIME_SOURCE, _RANDOM_SOURCE
    if time_source is not None:
        _TIME_SOURCE = time_source
    if random_source is not None:
        _RANDOM_SOURCE = random_source


def reset_id_sources() -> None:
    global _TIME_SOURCE, _RANDOM_SOURCE
    _TIME_SOURCE = time.time
    _RANDOM_SOURCE = os.urandom
