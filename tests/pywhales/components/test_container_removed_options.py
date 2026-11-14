"""Container options removed for Docker CLI 29.9.3 are rejected as unknown keywords."""

from pathlib import Path

import pytest

from pywhales.components.container.cli_wrapper import RunArgs
from tests.pywhales.components.argv_fakes import calls, make_fake_cli

REMOVED = ["content_trust", "kernel_memory", "service_ports"]


@pytest.mark.parametrize(
    ("method", "keyword"),
    [
        *[(method, keyword) for method in ("create", "run") for keyword in REMOVED],
        ("update", "kernel_memory"),
    ],
)
def test_removed_keywords_are_rejected_before_any_launch(
    tmp_path: Path, method: str, keyword: str
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(TypeError):
        getattr(client.container, method)("web", **{keyword: True})
    assert calls(log) == []


def test_run_options_typed_dict_drops_the_removed_keys() -> None:
    assert not set(REMOVED) & RunArgs.__annotations__.keys()
