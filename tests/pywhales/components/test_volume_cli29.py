"""Docker CLI 29.9.3 volume options asserted as exact argv; never launches Docker."""

import math
from pathlib import Path

import pytest

from pywhales import Volume
from pywhales.exceptions import DockerException, DockerTimeout
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

INSPECT_RULE = Rule(
    match=("inspect",), stdout='[{"Name": "vol", "Driver": "local", "Scope": "local"}]'
)


def test_create_forwards_cluster_options_after_the_legacy_ones(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("create",), stdout="vol\n"), INSPECT_RULE]
    )
    client.volume.create(
        "vol",
        "csi",
        {"k": "v"},
        {"o": "p"},
        type="mount",
        scope="multi",
        sharing="readonly",
        availability="pause",
        group="grp",
        secrets={"password": "secret-a", "token": "secret-b"},
        limit_bytes=1048576,
        required_bytes="2g",
        topology_preferred=["zone=a", "zone=b,rack=1"],
        topology_required=["zone=a"],
    )
    assert calls(log)[0] == [
        "volume",
        "create",
        "--driver",
        "csi",
        "--label",
        "k=v",
        "--opt",
        "o=p",
        "--availability",
        "pause",
        "--group",
        "grp",
        "--limit-bytes",
        "1048576",
        "--required-bytes",
        "2g",
        "--scope",
        "multi",
        "--secret",
        "password=secret-a",
        "--secret",
        "token=secret-b",
        "--sharing",
        "readonly",
        "--topology-preferred",
        "zone=a",
        "--topology-preferred",
        "zone=b,rack=1",
        "--topology-required",
        "zone=a",
        "--type",
        "mount",
        "vol",
    ]


def test_create_keeps_zero_byte_limits(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("create",), stdout="vol\n"), INSPECT_RULE]
    )
    client.volume.create("vol", limit_bytes=0, required_bytes=0)
    assert calls(log)[0] == [
        "volume",
        "create",
        "--limit-bytes",
        "0",
        "--required-bytes",
        "0",
        "vol",
    ]


def test_create_without_cluster_options_keeps_legacy_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("create",), stdout="vol\n"), INSPECT_RULE]
    )
    client.volume.create("vol")
    assert calls(log)[0] == ["volume", "create", "vol"]


def test_list_cluster_forwards_the_flag_before_filters(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("list",), stdout="a\nb\n")])
    volumes = client.volume.list([("driver", "local")], cluster=True)
    assert [str(volume) for volume in volumes] == ["a", "b"]
    assert calls(log) == [
        ["volume", "list", "--quiet", "--cluster", "--filter", "driver=local"]
    ]


def test_default_list_keeps_legacy_argv_and_stays_lazy(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("list",), stdout="a\n")])
    client.volume.list()
    assert calls(log) == [["volume", "list", "--quiet"]]


def test_hydrated_cluster_list_batch_inspects_the_listed_names(tmp_path: Path) -> None:
    snapshots = '[{"Name": "a", "Driver": "csi"}, {"Name": "b", "Driver": "csi"}]'
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("list",), stdout="a\nb\n"),
            Rule(match=("inspect",), stdout=snapshots),
        ],
    )
    volumes = client.volume.list(cluster=True, hydrate=True)
    assert [volume.driver for volume in volumes] == ["csi", "csi"]
    assert calls(log) == [
        ["volume", "list", "--quiet", "--cluster"],
        ["volume", "inspect", "a", "b"],
    ]


def test_remove_force_precedes_the_names(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.volume.remove(["a", "b"], force=True)
    assert calls(log) == [["volume", "remove", "--force", "a", "b"]]


def test_remove_without_force_keeps_legacy_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.volume.remove("a")
    assert calls(log) == [["volume", "remove", "a"]]


def test_handle_remove_forwards_force(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Volume(client.client_config, "vol", is_immutable_id=True).remove(force=True)
    assert calls(log) == [["volume", "remove", "--force", "vol"]]


def test_empty_forced_removal_skips_the_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.volume.remove([], force=True)
    assert calls(log) == []


def test_update_forwards_availability_to_one_volume(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.volume.update("vol", availability="drain") is None
    assert calls(log) == [["volume", "update", "--availability", "drain", "vol"]]


def test_handle_update_forwards_availability(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    volume = Volume(client.client_config, "vol", is_immutable_id=True)
    assert volume.update(availability="pause") is None
    assert calls(log) == [["volume", "update", "--availability", "pause", "vol"]]


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, True])
def test_update_rejects_an_invalid_timeout_before_launch(
    tmp_path: Path, timeout: float
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.volume.update("vol", availability="active", timeout=timeout)
    assert calls(log) == []


def test_update_timeout_is_a_local_deadline(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("update",), sleep=30)])
    with pytest.raises(DockerTimeout):
        client.volume.update("vol", availability="active", timeout=0.3)
    assert calls(log) == [["volume", "update", "--availability", "active", "vol"]]


@pytest.mark.parametrize("exit_code", [0, 1], ids=["success", "failure"])
def test_update_releases_the_pin_of_a_hydrated_handle(
    tmp_path: Path, exit_code: int
) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("list",), stdout="vol\n"),
            INSPECT_RULE,
            Rule(match=("update",), exit_code=exit_code, stderr="refused"),
        ],
    )
    (volume,) = client.volume.list(hydrate=True)
    assert volume.driver == "local"
    assert len(calls(log)) == 2
    if exit_code:
        with pytest.raises(DockerException):
            volume.update(availability="drain")
    else:
        volume.update(availability="drain")
    assert volume.driver == "local"
    assert calls(log)[2:] == [
        ["volume", "update", "--availability", "drain", "vol"],
        ["volume", "inspect", "vol"],
    ]
