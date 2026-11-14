"""Self-test for the recording fake CLI used by exact-argv tests."""

from pathlib import Path

import pytest

from pywhales.client_config import Command
from pywhales.exceptions import DockerException
from pywhales.utils import run
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli, records


def test_records_argv_and_replays_matching_rule(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("version",), stdout="v1\n"),
            Rule(match=("fail",), stderr="boom\n", exit_code=7),
            Rule(match=("stdin",), read_stdin=True),
        ],
    )
    assert run(Command([*client.docker_cmd, "version"])) == "v1"
    with pytest.raises(DockerException) as error:
        run(Command([*client.docker_cmd, "fail", "--x"]))
    assert error.value.return_code == 7
    assert error.value.stderr == "boom\n"
    run(Command([*client.docker_cmd, "stdin"]), input=b"abc")
    run(Command([*client.docker_cmd, "other"]))
    assert calls(log) == [["version"], ["fail", "--x"], ["stdin"], ["other"]]
    assert records(log)[2]["stdin_bytes"] == 3


def test_empty_log_has_no_calls(tmp_path: Path) -> None:
    _, log = make_fake_cli(tmp_path)
    assert calls(log) == []
    assert records(log) == []
