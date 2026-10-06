"""Opaque keyset cursors for telemetry pages.

A cursor is a *position*, and the platform refuses to treat anything else as one. The encoding is
base64url over ``{"ts": <ISO instant>, "id": <row id>}`` — deliberately readable to a developer and
deliberately opaque to a client, which must not construct one by hand or infer an order from it.

Two rules the timeline learned first and this module keeps:

* a malformed cursor is a **validation failure**, never a silent restart from the beginning. A client
  that sent a corrupt cursor and received the first page back would loop forever, and would look like a
  UI bug rather than a refusal;
* the cursor carries the *instant plus the id*, because instants tie. A cursor over a timestamp alone
  cannot express "after this row" when three points share it, and the page boundary would skip or repeat
  rows depending on which way the database happened to order them.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json

from drillai.core.errors import ValidationFailed


def encode_cursor(ts: dt.datetime, row_id: str) -> str:
    """The position of one row as an opaque string."""

    moment = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    payload = json.dumps({"ts": moment.astimezone(dt.UTC).isoformat(), "id": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[dt.datetime, str]:
    """The instant and id a cursor names, or a validation failure that says which part is wrong."""

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValidationFailed(
            "the cursor is not a position this API produced",
            details={"field": "cursor", "value": cursor[:64]},
        ) from None
    if not isinstance(data, dict) or "ts" not in data or "id" not in data:
        raise ValidationFailed(
            "the cursor does not name a position",
            details={"field": "cursor", "value": cursor[:64]},
        )
    try:
        moment = dt.datetime.fromisoformat(str(data["ts"]))
    except ValueError:
        raise ValidationFailed(
            "the cursor's instant is not a timestamp",
            details={"field": "cursor", "value": cursor[:64]},
        ) from None
    if moment.tzinfo is None:
        raise ValidationFailed(
            "the cursor's instant has no timezone, so it cannot be compared with stored measurements",
            details={"field": "cursor", "value": cursor[:64]},
        )
    return moment, str(data["id"])
