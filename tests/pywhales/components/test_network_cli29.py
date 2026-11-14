"""Docker CLI 29.9.3 network options asserted as exact argv; never launches Docker."""

import json
from pathlib import Path

import pytest

from pywhales import Network
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli


def test_connect_forwards_gateway_priority_and_link_local_ips(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.connect(
        "net",
        "ctr",
        "web",
        ["k=v"],
        "10.0.0.2",
        "fd00::2",
        ["other"],
        gw_priority=10,
        link_local_ips=["169.254.10.10", "169.254.10.11"],
    )
    assert calls(log) == [
        [
            "network",
            "connect",
            "--alias",
            "web",
            "--driver-opt",
            "k=v",
            "--gw-priority",
            "10",
            "--ip",
            "10.0.0.2",
            "--ip6",
            "fd00::2",
            "--link",
            "other",
            "--link-local-ip",
            "169.254.10.10",
            "--link-local-ip",
            "169.254.10.11",
            "net",
            "ctr",
        ]
    ]


@pytest.mark.parametrize("priority", [-5, 0, 7])
def test_connect_keeps_every_integer_gateway_priority(
    tmp_path: Path, priority: int
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.connect("net", "ctr", gw_priority=priority)
    assert calls(log) == [
        ["network", "connect", "--gw-priority", str(priority), "net", "ctr"]
    ]


def test_connect_without_new_options_keeps_legacy_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.connect("net", "ctr")
    assert calls(log) == [["network", "connect", "net", "ctr"]]


def test_create_forwards_config_ingress_and_scope_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout="net-id\n")])
    network = client.network.create(
        "child",
        config_from="template",
        config_only=True,
        ingress=True,
        ipv4=False,
        enable_ipv6=True,
        scope="swarm",
    )
    assert network.id == "net-id"
    assert calls(log) == [
        [
            "network",
            "create",
            "--config-from",
            "template",
            "--config-only",
            "--ingress",
            "--ipv4=false",
            "--ipv6",
            "--scope",
            "swarm",
            "child",
        ]
    ]


@pytest.mark.parametrize(
    ("ipv4", "expected"),
    [(True, ["--ipv4"]), (False, ["--ipv4=false"]), (None, [])],
    ids=["true", "false", "omitted"],
)
def test_create_encodes_ipv4_like_ipv6(
    tmp_path: Path, ipv4: bool | None, expected: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout="net-id\n")])
    client.network.create("net", ipv4=ipv4)
    assert calls(log) == [["network", "create", *expected, "net"]]


@pytest.mark.parametrize("scope", ["global", "LOCAL", "", 1])
def test_create_rejects_unknown_scope_before_launch(
    tmp_path: Path, scope: object
) -> None:
    client, log = make_fake_cli(tmp_path)
    kwargs = json.loads(json.dumps({"scope": scope}))
    with pytest.raises(ValueError):
        client.network.create("net", **kwargs)
    assert calls(log) == []


@pytest.mark.parametrize("value", [0, 1, "true"])
def test_create_rejects_non_boolean_ipv4_before_launch(
    tmp_path: Path, value: object
) -> None:
    client, log = make_fake_cli(tmp_path)
    kwargs = json.loads(json.dumps({"ipv4": value}))
    with pytest.raises(TypeError):
        client.network.create("net", **kwargs)
    assert calls(log) == []


def test_inspect_verbose_returns_raw_text_for_one_reference(tmp_path: Path) -> None:
    raw = '[\n    {"Name": "net", "Services": {}}\n]'
    client, log = make_fake_cli(tmp_path, [Rule(match=("inspect",), stdout=raw + "\n")])
    result = client.network.inspect("net", verbose=True)
    assert result == raw
    assert calls(log) == [["network", "inspect", "--verbose", "net"]]


def test_inspect_verbose_uses_one_launch_for_many_references(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("inspect",), stdout="[]")])
    assert client.network.inspect(["a", "b"], verbose=True) == "[]"
    assert calls(log) == [["network", "inspect", "--verbose", "a", "b"]]


def test_inspect_verbose_with_no_references_skips_the_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.network.inspect([], verbose=True) == ""
    assert calls(log) == []


def test_default_inspect_keeps_the_typed_handle_and_argv(tmp_path: Path) -> None:
    snapshot = '[{"Id": "net-id", "Name": "net"}]'
    client, log = make_fake_cli(tmp_path, [Rule(match=("inspect",), stdout=snapshot)])
    assert isinstance(client.network.inspect("net"), Network)
    assert isinstance(client.network.inspect("net", verbose=False), Network)
    assert calls(log) == [["network", "inspect", "net"]] * 2


def test_remove_force_precedes_the_references(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.remove(["a", "b"], force=True)
    assert calls(log) == [["network", "remove", "--force", "a", "b"]]


def test_remove_without_force_keeps_legacy_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.remove("a")
    assert calls(log) == [["network", "remove", "a"]]


def test_handle_remove_forwards_force(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Network(client.client_config, "net-id", is_immutable_id=True).remove(force=True)
    assert calls(log) == [["network", "remove", "--force", "net-id"]]


def test_empty_forced_removal_skips_the_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.network.remove([], force=True)
    assert calls(log) == []
