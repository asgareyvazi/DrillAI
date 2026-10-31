from __future__ import annotations

import pytest

from drillai.api.routers.documents import _read_upload_bounded
from drillai.core.errors import ValidationFailed


class ChunkedUpload:
    def __init__(self, data: bytes, chunk_size: int = 2) -> None:
        self.data = data
        self.chunk_size = chunk_size
        self.offset = 0
        self.requested_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.requested_sizes.append(size)
        size = min(size, self.chunk_size)
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


async def test_upload_reader_stops_at_first_byte_over_the_limit() -> None:
    file = ChunkedUpload(b"0123456789", chunk_size=2)

    with pytest.raises(ValidationFailed) as raised:
        await _read_upload_bounded(file, max_bytes=5)

    assert raised.value.details == {"bytes": 6, "limit": 5}
    assert file.offset == 6, "the reader should not buffer the rest of an oversized upload"
    assert all(size <= 6 for size in file.requested_sizes)


async def test_upload_reader_accepts_a_file_at_the_exact_limit() -> None:
    file = ChunkedUpload(b"01234", chunk_size=2)

    assert await _read_upload_bounded(file, max_bytes=5) == b"01234"
    assert file.offset == 5
