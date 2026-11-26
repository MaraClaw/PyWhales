import os
import sys
from types import GeneratorType
from unittest.mock import patch

import pytest

from pywhales import DockerClient, _archive_deadline
from pywhales.telemetry import CompletionRecord, TelemetryRecord


def test_default_save_reads_binary_blocks_in_bulk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given long binary lines that must not be read one syscall per byte.
    records: list[TelemetryRecord] = []
    payload = b"x" * (128 * 1024) + b"\n\x00\xff\nlast"
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            r"import sys; sys.stdout.buffer.write(b'x'*(128*1024)+b'\n\x00\xff\nlast')",
        ],
        telemetry_observer=records.append,
    )
    read = os.read
    sizes: list[int] = []

    def measure(fd: int, size: int) -> bytes:
        chunk = read(fd, size)
        sizes.append(len(chunk))
        return chunk

    monkeypatch.setattr(_archive_deadline.os, "read", measure)

    # When consuming the lazy save through its public API.
    with patch.object(client.image, "inspect", return_value=[]):
        result = list(client.image.save("sample"))

    # Then raw reads are bulk-sized and counted exactly once, regardless of newlines.
    assert result == [payload[:65536], payload[65536:131072], payload[131072:]]
    assert sum(sizes) == len(payload)
    assert len(sizes) < len(payload) // 100
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert completed.stdout_bytes == len(payload)


def test_save_close_counts_only_blocks_actually_read() -> None:
    # Given more than one block, with newlines inside the first.
    records: list[TelemetryRecord] = []
    block = b"a\n" * 32768
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import sys,threading; sys.stdout.buffer.write(b'a\\n'*32768 + b'remaining'); "
            "sys.stdout.flush(); threading.Event().wait()",
        ],
        telemetry_observer=records.append,
    )
    with patch.object(client.image, "inspect", return_value=[]):
        archive = client.image.save("sample")
    assert isinstance(archive, GeneratorType)
    try:
        # When closing after yielding only the first full block.
        assert next(archive) == block
    finally:
        archive.close()

    # Then telemetry includes every byte actually read, not unread trailing output.
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdout_bytes) == ("closed", len(block))
