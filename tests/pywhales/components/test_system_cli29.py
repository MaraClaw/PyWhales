"""Docker CLI 29.9.3 system options asserted as exact argv; never launches Docker."""

import json
from pathlib import Path

import pytest

from pywhales.components.system.cli_wrapper import DiskFreeResult
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

SUMMARY = "\n".join(
    json.dumps(
        {
            "Type": kind,
            "TotalCount": "4",
            "Active": "2",
            "Size": "1.5GB",
            "Reclaimable": "500MB",
        }
    )
    for kind in ("Images", "Containers", "Local Volumes", "Build Cache")
)
VERBOSE_REPORT = (
    "Images space usage:\n\nREPOSITORY   TAG   IMAGE ID\nalpine       3     abc\n"
)


def test_disk_free_verbose_returns_the_cli_text_unparsed(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("df",), stdout=VERBOSE_REPORT)])

    result = client.system.disk_free(verbose=True)

    assert result == VERBOSE_REPORT.removesuffix("\n")
    assert calls(log) == [["system", "df", "--verbose"]]


@pytest.mark.parametrize("options", [{}, {"verbose": False}], ids=["default", "false"])
def test_disk_free_without_verbose_keeps_the_typed_summary(
    tmp_path: Path, options: dict[str, bool]
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("df",), stdout=SUMMARY)])

    result = client.system.disk_free(**options)

    assert isinstance(result, DiskFreeResult)
    assert result.images.total_count == 4
    assert calls(log) == [["system", "df", "--format", "{{json .}}"]]


def test_root_events_alias_streams_system_events(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    with client.events(filters={"type": "container"}) as events:
        assert list(events) == []

    assert calls(log) == [
        ["system", "events", "--format", "{{json .}}", "--filter", "type=container"]
    ]
