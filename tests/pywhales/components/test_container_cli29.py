"""Docker CLI 29.9.3 container options asserted as exact argv; never launches Docker."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from pywhales import Container, DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.container.cli_wrapper import PublishedPort
from pywhales.exceptions import DockerException
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

CONTAINER_ID = "a" * 64
SIZED_ENTRY = {"Id": CONTAINER_ID, "Name": "/web", "SizeRw": 1024, "SizeRootFs": 4096}
SIZED_INSPECT = json.dumps([SIZED_ENTRY])
PLAIN_INSPECT = json.dumps([{"Id": CONTAINER_ID, "Name": "/web"}])
ALL_PORTS = """\
53/udp -> 0.0.0.0:51095
53/udp -> [::]:51095
80/tcp -> 127.0.0.1:60237
8080/tcp -> 0.0.0.0:60238
8080/tcp -> [::]:60238
9090/tcp -> [::1]:60236
"""


def handle(client: DockerClient) -> Container:
    return Container(client.client_config, "cid", is_immutable_id=True)


def test_create_forwards_new_options_in_order(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout="cid\n")])
    client.container.create(
        "alpine",
        annotations={"a": "1", "b": "2"},
        cpu_count=2,
        cpu_percent=50,
        io_maxbandwidth="10mb",
        io_maxiops=100,
        pull="never",
        quiet=True,
        umask="022",
        use_api_socket=True,
    )
    assert calls(log) == [
        [
            "create",
            "--annotation",
            "a=1",
            "--annotation",
            "b=2",
            "--cpu-count",
            "2",
            "--cpu-percent",
            "50",
            "--io-maxbandwidth",
            "10mb",
            "--io-maxiops",
            "100",
            "--pull",
            "never",
            "--quiet",
            "--umask",
            "022",
            "--use-api-socket",
            "alpine",
        ]
    ]


def test_run_forwards_new_options_in_order(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.run(
        "alpine",
        ["ls"],
        annotations={"a": "1"},
        attach=["stdout", "stderr"],
        cpu_count=2,
        cpu_percent=50,
        detach_keys="ctrl-x",
        io_maxbandwidth=1048576,
        io_maxiops=100,
        pull="never",
        quiet=True,
        umask="022",
        use_api_socket=True,
    )
    assert calls(log) == [
        [
            "container",
            "run",
            "--annotation",
            "a=1",
            "--attach",
            "stdout",
            "--attach",
            "stderr",
            "--cpu-count",
            "2",
            "--cpu-percent",
            "50",
            "--detach-keys",
            "ctrl-x",
            "--io-maxbandwidth",
            "1048576",
            "--io-maxiops",
            "100",
            "--pull",
            "never",
            "--quiet",
            "--umask",
            "022",
            "--use-api-socket",
            "alpine",
            "ls",
        ]
    ]


def test_run_attach_accepts_one_stream_name(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.run("alpine", attach="stdin", pull="never")
    assert calls(log) == [
        ["container", "run", "--attach", "stdin", "--pull", "never", "alpine"]
    ]


def test_new_create_and_run_options_are_omitted_by_default(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout="cid\n")])
    client.container.create("alpine", pull="never")
    client.container.run("alpine", pull="never")
    assert calls(log) == [
        ["create", "--pull", "never", "alpine"],
        ["container", "run", "--pull", "never", "alpine"],
    ]


def test_zero_valued_numeric_options_are_kept(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout="cid\n")])
    client.container.create(
        "alpine", cpu_count=0, cpu_percent=0, io_maxiops=0, pull="never"
    )
    assert calls(log)[0] == [
        "create",
        "--cpu-count",
        "0",
        "--cpu-percent",
        "0",
        "--io-maxiops",
        "0",
        "--pull",
        "never",
        "alpine",
    ]


def test_commit_forwards_changes_and_no_pause(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("commit",), stdout="sha256:abc\n")]
    )
    client.container.commit(
        "web", "repo:tag", "me", "msg", False, changes=["ENV A=1", "CMD x"]
    )
    assert calls(log)[0] == [
        "container",
        "commit",
        "--author",
        "me",
        "--change",
        "ENV A=1",
        "--change",
        "CMD x",
        "--message",
        "msg",
        "--no-pause",
        "web",
        "repo:tag",
    ]


def test_commit_never_emits_pause_and_keeps_a_single_change_whole(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("commit",), stdout="sha256:abc\n")]
    )
    client.container.commit("web")
    handle(client).commit("repo:tag", changes="ENV A=1", pause=True)
    assert calls(log) == [
        ["container", "commit", "web"],
        ["container", "commit", "--change", "ENV A=1", "cid", "repo:tag"],
    ]


def test_handle_commit_forwards_changes_and_no_pause(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("commit",), stdout="sha256:abc\n")]
    )
    handle(client).commit(pause=False, changes=("ENV A=1",))
    assert calls(log) == [
        ["container", "commit", "--change", "ENV A=1", "--no-pause", "cid"]
    ]


def test_copy_forwards_flags_before_the_endpoints(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.copy("src", ("web", "/dst"))
    client.container.copy(
        ("web", "/src"), "dst", archive=True, follow_link=True, quiet=True
    )
    assert calls(log) == [
        ["container", "cp", "src", "web:/dst"],
        ["container", "cp", "--archive", "--follow-link", "--quiet", "web:/src", "dst"],
    ]


def test_handle_copies_forward_flags(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    container = handle(client)
    container.copy_from("/src", "dst", archive=True, quiet=True)
    container.copy_to("src", "/dst", follow_link=True)
    assert calls(log) == [
        ["container", "cp", "--archive", "--quiet", "cid:/src", "dst"],
        ["container", "cp", "--follow-link", "src", "cid:/dst"],
    ]


def test_copy_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.container.copy("src", ("web", "/dst"), archive=True, timeout=0)
    assert calls(log) == []


def test_scalar_inspect_without_size_keeps_its_plain_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("inspect",), stdout=PLAIN_INSPECT)]
    )
    assert client.container.inspect("web").id == CONTAINER_ID
    assert calls(log) == [["container", "inspect", "web"]]


def test_inspect_size_launches_for_scalar_and_pins_the_sizes(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("--size",), stdout=SIZED_INSPECT),
            Rule(match=("inspect",), stdout=PLAIN_INSPECT),
        ],
    )
    with patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 0):
        container = client.container.inspect("web", size=True)
        assert container.id == CONTAINER_ID
        assert container.size_rw == 1024
        assert container.size_root_fs == 4096
        assert calls(log) == [["container", "inspect", "--size", "web"]]

        container.stop()
        assert container.size_rw is None
    assert calls(log)[-1] == ["container", "inspect", CONTAINER_ID]


def test_inspect_size_batches_iterables_in_one_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("--size",), stdout=json.dumps([SIZED_ENTRY] * 2))]
    )
    containers = client.container.inspect(["web", "web"], size=True)
    assert [container.size_rw for container in containers] == [1024, 1024]
    assert calls(log) == [["container", "inspect", "--size", "web", "web"]]


def test_inspect_without_size_batches_unpinned_snapshots(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("inspect",), stdout=PLAIN_INSPECT)]
    )
    with patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 0):
        container = client.container.inspect(["web"])[0]
        assert container.size_rw is None
    assert calls(log) == [
        ["container", "inspect", "web"],
        ["container", "inspect", CONTAINER_ID],
    ]


def test_inspect_size_rejects_a_snapshot_for_another_container(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("--size",), stdout=SIZED_INSPECT)])
    with pytest.raises(ParsingError):
        client.container.inspect("other", size=True)


def test_list_forwards_last_and_latest(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("list",), stdout="a\nb\n")])
    assert [str(c) for c in client.container.list(last=2)] == ["a", "b"]
    client.container.list(True, [("status", "exited")], latest=True)
    client.container.list(last=0)
    client.container.list()
    base = ["container", "list", "-q", "--no-trunc"]
    assert calls(log) == [
        [*base, "--last", "2"],
        [*base, "--all", "--filter", "status=exited", "--latest"],
        [*base, "--last", "0"],
        base,
    ]


def test_remove_forwards_link(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.remove("web", force=True, link=True, volumes=True)
    client.container.remove("web", link=True)
    handle(client).remove(link=True)
    assert calls(log) == [
        ["container", "rm", "--force", "--link", "--volumes", "web"],
        ["container", "rm", "--link", "web"],
        ["container", "rm", "--link", "cid"],
    ]


def test_stop_emits_timeout_and_signal(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.stop("web")
    client.container.stop("web", 5, signal="SIGKILL")
    client.container.stop(["a", "b"], 0)
    client.container.stop("web", timedelta(seconds=2.9), signal=15)
    assert calls(log) == [
        ["container", "stop", "web"],
        ["container", "stop", "--signal", "SIGKILL", "--timeout", "5", "web"],
        ["container", "stop", "--timeout", "0", "a", "b"],
        ["container", "stop", "--signal", "SIGTERM", "--timeout", "2", "web"],
    ]
    assert not any("--time" in argv for argv in calls(log))


def test_restart_emits_timeout_and_signal(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.restart("web")
    client.container.restart("web", 5, signal="KILL")
    client.container.restart("web", timedelta(minutes=1))
    assert calls(log) == [
        ["restart", "web"],
        ["restart", "--signal", "SIGKILL", "--timeout", "5", "web"],
        ["restart", "--timeout", "60", "web"],
    ]


def test_handles_forward_timeout_and_signal(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    container = handle(client)
    container.stop(3, signal="SIGINT")
    container.restart(timedelta(seconds=4), signal="SIGHUP")
    container.stop()
    assert calls(log) == [
        ["container", "stop", "--signal", "SIGINT", "--timeout", "3", "cid"],
        ["restart", "--signal", "SIGHUP", "--timeout", "4", "cid"],
        ["container", "stop", "cid"],
    ]


def test_start_forwards_checkpoint_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.start(
        "web", detach_keys="ctrl-x", checkpoint="cp1", checkpoint_dir=tmp_path / "cp"
    )
    handle(client).start(checkpoint="cp2", checkpoint_dir="/var/cp")
    client.container.start("web", checkpoint="cp3", checkpoint_dir=None)
    assert calls(log) == [
        [
            "container",
            "start",
            "--checkpoint",
            "cp1",
            "--checkpoint-dir",
            str(tmp_path / "cp"),
            "--detach-keys",
            "ctrl-x",
            "web",
        ],
        [
            "container",
            "start",
            "--checkpoint",
            "cp2",
            "--checkpoint-dir",
            "/var/cp",
            "cid",
        ],
        ["container", "start", "--checkpoint", "cp3", "web"],
    ]


def test_attached_start_keeps_checkpoint_options_after_attach(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.container.start("web", True, True, checkpoint="cp1", timeout=5)
    assert calls(log) == [
        [
            "container",
            "start",
            "--attach",
            "--checkpoint",
            "cp1",
            "--interactive",
            "web",
        ]
    ]


def test_start_validates_timeout_before_checkpoint_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.container.start("web", checkpoint="cp1", timeout=0)
    with pytest.raises(ValueError):
        handle(client).start(checkpoint="cp1", timeout=-1)
    assert calls(log) == []


def test_port_parses_every_binding_including_ipv6(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("port",), stdout=ALL_PORTS)])
    assert client.container.port("web") == [
        PublishedPort("53/udp", "0.0.0.0", 51095),
        PublishedPort("53/udp", "::", 51095),
        PublishedPort("80/tcp", "127.0.0.1", 60237),
        PublishedPort("8080/tcp", "0.0.0.0", 60238),
        PublishedPort("8080/tcp", "::", 60238),
        PublishedPort("9090/tcp", "::1", 60236),
    ]
    assert calls(log) == [["container", "port", "web"]]


@pytest.mark.parametrize(
    ("private_port", "protocol", "requested", "expected_port"),
    [
        (80, None, "80", "80/tcp"),
        ("80/tcp", None, "80/tcp", "80/tcp"),
        (53, "udp", "53/udp", "53/udp"),
        ("53", "sctp", "53/sctp", "53/sctp"),
    ],
)
def test_port_with_a_private_port_parses_address_only_lines(
    tmp_path: Path,
    private_port: int | str,
    protocol: str | None,
    requested: str,
    expected_port: str,
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("port",), stdout="0.0.0.0:51095\n[::]:51095\n")]
    )
    assert client.container.port("web", private_port, protocol) == [
        PublishedPort(expected_port, "0.0.0.0", 51095),
        PublishedPort(expected_port, "::", 51095),
    ]
    assert calls(log) == [["container", "port", "web", requested]]


def test_port_returns_empty_when_nothing_is_published(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path)
    assert client.container.port("web") == []


def test_port_handle_forwards_arguments(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("port",), stdout="127.0.0.1:60237\n")]
    )
    assert handle(client).port(80, "tcp") == [
        PublishedPort("80/tcp", "127.0.0.1", 60237)
    ]
    assert calls(log) == [["container", "port", "cid", "80/tcp"]]


def test_root_port_alias_forwards_to_the_container_group(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("port",), stdout="127.0.0.1:60237\n")]
    )
    assert client.port("web", 80) == [PublishedPort("80/tcp", "127.0.0.1", 60237)]
    assert calls(log) == [["container", "port", "web", "80"]]


def test_published_port_is_a_hashable_value() -> None:
    binding = PublishedPort("80/tcp", "127.0.0.1", 1)
    assert {binding: 1}[PublishedPort("80/tcp", "127.0.0.1", 1)] == 1


@pytest.mark.parametrize(
    ("private_port", "protocol"), [(None, "tcp"), ("80/tcp", "tcp")]
)
def test_port_rejects_inconsistent_protocol_before_lookup(
    tmp_path: Path, private_port: str | None, protocol: str
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.container.port("web", private_port, protocol)
    assert calls(log) == []


@pytest.mark.parametrize(
    "stdout",
    [
        "80/tcp -> nonsense\n",
        "80/tcp 0.0.0.0:80\n",
        "80/tcp -> 0.0.0.0:port\n",
        "80/tcp -> 0.0.0.0:\n",
    ],
)
def test_port_rejects_malformed_output(tmp_path: Path, stdout: str) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("port",), stdout=stdout)])
    with pytest.raises(ParsingError):
        client.container.port("web")


def test_port_for_an_unpublished_port_raises_the_cli_error(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("port",),
                stderr="no public port '9999' published for web\n",
                exit_code=1,
            )
        ],
    )
    with pytest.raises(DockerException):
        client.container.port("web", 9999)
