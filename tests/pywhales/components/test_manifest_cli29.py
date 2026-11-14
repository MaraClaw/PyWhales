"""Docker CLI 29.9.3 manifest commands asserted as exact argv; never launches Docker."""

from pathlib import Path

import pytest

from pywhales.components.manifest.cli_wrapper import ManifestList
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

VERBOSE_TEXT = '[{"Ref": "alpine@sha256:abc", "Descriptor": {"platform": {}}}]'
LIST_JSON = '{"schemaVersion": 2, "mediaType": "list", "manifests": []}'
INSPECT_RULE = Rule(match=("inspect",), stdout=LIST_JSON)


@pytest.mark.parametrize(
    ("insecure", "flags"),
    [(False, []), (True, ["--insecure"])],
    ids=["secure", "insecure"],
)
def test_inspect_verbose_returns_the_cli_text_unparsed(
    tmp_path: Path, insecure: bool, flags: list[str]
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("inspect",), stdout=VERBOSE_TEXT + "\n")]
    )

    result = client.manifest.inspect("alpine", insecure, verbose=True)

    assert result == VERBOSE_TEXT
    assert calls(log) == [["manifest", "inspect", "alpine", *flags, "--verbose"]]


@pytest.mark.parametrize("options", [{}, {"verbose": False}], ids=["default", "false"])
def test_inspect_without_verbose_keeps_the_handle(
    tmp_path: Path, options: dict[str, bool]
) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    handle = client.manifest.inspect("alpine", **options)

    assert isinstance(handle, ManifestList)
    assert calls(log) == [["manifest", "inspect", "alpine"]]


def test_push_forwards_insecure_to_the_preflight_and_the_push(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    client.manifest.push("registry.test/app:1", purge=True, insecure=True)

    assert calls(log) == [
        ["manifest", "inspect", "registry.test/app:1", "--insecure"],
        ["manifest", "push", "--insecure", "--purge", "registry.test/app:1"],
    ]


def test_push_defaults_keep_the_secure_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    client.manifest.push("registry.test/app:1")

    assert calls(log) == [
        ["manifest", "inspect", "registry.test/app:1"],
        ["manifest", "push", "registry.test/app:1"],
    ]
