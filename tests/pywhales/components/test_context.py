import json
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.context.cli_wrapper import Context, DockerContextConfig
from pywhales.components.context.models import ContextEndpoint, ContextInspectResult
from pywhales.exceptions import DockerException, NoSuchContainer, NotASwarmManager
from pywhales.test_utils import get_all_jsons


@pytest.mark.parametrize("json_file", get_all_jsons("contexts"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    ContextInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


def test_create_context(docker_client: DockerClient) -> None:
    testname = "testpow"
    host = "ssh://test@test.domain"
    description = "PyWhales testing context"

    all_contexts_before = set(docker_client.context.list())
    with docker_client.context.create(
        testname, docker={"host": host}, description=description
    ) as new_context:
        assert new_context.name == testname
        assert new_context.endpoints is not None
        assert new_context.endpoints["docker"].host == host
        assert new_context.metadata is not None
        assert new_context.metadata["Description"] == description

        assert new_context not in all_contexts_before

        assert new_context in docker_client.context.list()


def test_inpect(docker_client: DockerClient) -> None:
    default_context = docker_client.context.inspect()
    assert default_context.name == "default"
    assert default_context == docker_client.context.inspect("default")
    a, b = docker_client.context.inspect(["default", "default"])
    assert a == b


def test_list_contexts(docker_client: DockerClient) -> None:
    assert docker_client.context.list() == [docker_client.context.inspect("default")]


def test_use_context(docker_client: DockerClient) -> None:
    docker_client.context.use("default")


def test_use_context_returns(docker_client: DockerClient) -> None:
    assert docker_client.context.use("default") == docker_client.context.inspect(
        "default"
    )


def test_remove_empty_context_list(docker_client: DockerClient) -> None:
    all_contexts = set(docker_client.context.list())
    docker_client.context.remove([])
    assert all_contexts == set(docker_client.context.list())


def test_batch_inspect_preserves_context_order_and_duplicate_handles() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    references = ["second", "first", "second"]
    payload = [{"Name": reference} for reference in references]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, json.dumps(payload).encode(), b""),
    ) as command:
        resources = client.context.inspect(references)

    assert [resource.name for resource in resources] == references
    assert resources[0] is not resources[2]
    assert all(resource._inspect_result is not None for resource in resources)
    assert all(resource.client_config is client.client_config for resource in resources)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "--host",
        "inventory-host",
        "context",
        "inspect",
        *references,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "null",
        "[1, 2]",
        "[]",
        '[{"Name": "first"}]',
        '[{"Name": "first"}, {"Name": "first"}, {"Name": "first"}]',
        '[{"Name": "first"}, {"Name": 2}]',
        '[{"Name": "first"}, {"Name": ""}]',
        '[{"Name": "first"}, {}]',
        '[{"Name": "first"}, {"Name": "first-longer"}]',
        '[{"Name": "first"}, {"Name": "first", "Metadata": {"changed": true}}]',
    ],
)
def test_batch_inspect_validates_all_contexts_before_hydration(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ) as command,
        patch.object(Context, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.context.inspect(["first", "first"])
    command.assert_called_once()
    hydrate.assert_not_called()


def test_batch_inspect_empty_contexts_avoids_lookup() -> None:
    with patch("shutil.which") as lookup, patch("subprocess.run") as command:
        assert DockerClient().context.inspect([]) == []
    lookup.assert_not_called()
    command.assert_not_called()


def test_scalar_inspect_context_none_keeps_current_context_lookup() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, b'[{"Name": "current"}]', b""),
    ) as command:
        resource = client.context.inspect(None)
    assert resource.name == "current"
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "context", "inspect"]


EMPTY_SELECTIONS = [
    pytest.param(list, id="list"),
    pytest.param(tuple, id="tuple"),
    pytest.param(lambda: (item for item in ()), id="generator"),
]
SINGLE_SELECTIONS = [
    pytest.param(lambda: "alpha", id="str"),
    pytest.param(lambda: ["alpha"], id="list"),
    pytest.param(lambda: (item for item in ["alpha"]), id="generator"),
]


@pytest.mark.parametrize("make_empty", EMPTY_SELECTIONS)
def test_context_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.context.remove(make_empty(), force=True)


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_context_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.context.remove(make_selection(), force=True)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "context",
        "remove",
        "--force",
        "alpha",
    ]


@pytest.mark.parametrize("as_dict", [False, True], ids=["dataclass", "dict"])
def test_create_context_with_endpoint_options(as_dict: bool) -> None:
    client = DockerClient(host="origin")
    client.client_config._client_call_with_path = ["docker-test"]
    source = Context(client.client_config, "source", is_immutable_id=True)
    docker = (
        {
            "from_": source,
            "host": "ssh://example",
            "certificate_authority": Path("/certs/ca.pem"),
            "certificate": Path("/certs/client.pem"),
            "key": Path("/certs/key.pem"),
            "skip_tls_verify": True,
        }
        if as_dict
        else DockerContextConfig(
            from_=source,
            host="ssh://example",
            certificate_authority=Path("/certs/ca.pem"),
            certificate=Path("/certs/client.pem"),
            key=Path("/certs/key.pem"),
            skip_tls_verify=True,
        )
    )
    responses = [
        CompletedProcess([], 0, b"created\n", b""),
        CompletedProcess([], 0, b'[{"Name":"created"}]', b""),
    ]
    with patch("subprocess.run", side_effect=responses) as command:
        result = client.context.create(
            "created",
            description="test context",
            from_=source,
            docker=docker,
        )

    assert isinstance(result, Context)
    assert result.name == "created"
    assert result.client_config is client.client_config
    assert [call.args[0] for call in command.call_args_list] == [
        [
            "docker-test",
            "--host",
            "origin",
            "context",
            "create",
            "--description",
            "test context",
            "--from",
            "source",
            "--docker",
            "from=source,host=ssh://example,ca=/certs/ca.pem,cert=/certs/client.pem,key=/certs/key.pem,skip-tls-verify=True",
            "created",
        ],
        ["docker-test", "--host", "origin", "context", "inspect", "created"],
    ]


@pytest.mark.parametrize("empty_configs", [False, True], ids=["omitted", "empty"])
def test_create_context_omits_unspecified_options(empty_configs: bool) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    responses = [
        CompletedProcess([], 0, b"", b""),
        CompletedProcess([], 0, b'[{"Name":"minimal"}]', b""),
    ]
    with patch("subprocess.run", side_effect=responses) as command:
        result = client.context.create("minimal", docker={} if empty_configs else None)

    options = ["--docker", "skip-tls-verify=False"] if empty_configs else []
    assert result.name == "minimal"
    assert [call.args[0] for call in command.call_args_list] == [
        ["docker-test", "context", "create", *options, "minimal"],
        ["docker-test", "context", "inspect", "minimal"],
    ]


@pytest.mark.parametrize(
    ("output", "names"), [(b"", []), (b"second\nfirst\n", ["second", "first"])]
)
def test_context_list_returns_ordered_lazy_handles(
    output: bytes, names: list[str]
) -> None:
    client = DockerClient(host="origin")
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, output, b"")
    ) as command:
        contexts = client.context.list()
        assert [context.name for context in contexts] == names

    assert all(isinstance(context, Context) for context in contexts)
    assert all(context.client_config is client.client_config for context in contexts)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "--host",
        "origin",
        "context",
        "list",
        "--quiet",
    ]


@pytest.mark.parametrize("as_handle", [False, True], ids=["name", "handle"])
def test_context_use_inspects_selected_context(as_handle: bool) -> None:
    client = DockerClient(host="origin")
    client.client_config._client_call_with_path = ["docker-test"]
    selection = (
        Context(client.client_config, "selected", is_immutable_id=True)
        if as_handle
        else "selected"
    )
    responses = [
        CompletedProcess([], 0, b"selected\n", b""),
        CompletedProcess([], 0, b'[{"Name":"selected"}]', b""),
    ]
    with patch("subprocess.run", side_effect=responses) as command:
        result = client.context.use(selection)

    assert isinstance(result, Context)
    assert result.name == "selected"
    assert result.client_config is client.client_config
    assert [call.args[0] for call in command.call_args_list] == [
        ["docker-test", "--host", "origin", "context", "use", "selected"],
        ["docker-test", "--host", "origin", "context", "inspect", "selected"],
    ]


def test_context_handle_use_preserves_originating_configuration() -> None:
    client = DockerClient(host="origin")
    client.client_config._client_call_with_path = ["docker-test"]
    context = Context(client.client_config, "selected", is_immutable_id=True)
    responses = [
        CompletedProcess([], 0, b"", b""),
        CompletedProcess([], 0, b'[{"Name":"selected"}]', b""),
    ]
    with patch("subprocess.run", side_effect=responses) as command:
        assert context.use() is None
    assert [call.args[0] for call in command.call_args_list] == [
        ["docker-test", "--host", "origin", "context", "use", "selected"],
        ["docker-test", "--host", "origin", "context", "inspect", "selected"],
    ]


def test_context_inspect_exposes_typed_properties_and_repr() -> None:
    client = DockerClient(host="origin")
    client.client_config._client_call_with_path = ["docker-test"]
    payload = json.dumps(
        [
            {
                "Name": "remote",
                "Metadata": {"Description": "Remote endpoint"},
                "Endpoints": {
                    "docker": {"Host": "ssh://remote", "SkipTLSVerify": True}
                },
                "TLSMaterial": {"docker": ["ca.pem"]},
                "Storage": {"MetadataPath": "/meta", "TLSPath": "/tls"},
            }
        ]
    ).encode()
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, payload, b"")
    ) as command:
        context = client.context.inspect()
        assert context.metadata == {"Description": "Remote endpoint"}
        assert context.endpoints == {
            "docker": ContextEndpoint(host="ssh://remote", skip_tls_verify=True)
        }
        assert context.tls_material == {"docker": ["ca.pem"]}
        assert context.storage is not None
        assert context.storage.metadata_path == "/meta"
        assert context.storage.tls_path == "/tls"
        assert "remote" in repr(context)
        assert "ssh://remote" in repr(context)
    assert context.name == "remote"
    assert context.client_config is client.client_config
    assert command.call_args_list[0].args[0] == [
        "docker-test",
        "--host",
        "origin",
        "context",
        "inspect",
    ]


NOT_A_MANAGER = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    b"and try again."
)
CONTEXT_MISSING = (
    b'context "resource": context not found: open '
    b"/Users/me/.docker/contexts/meta/0123abcd/meta.json: no such file or directory"
)
CONTEXT_JSON = b'[{"Name":"resource"}]'
# Each failure below is not this context's own not-found error.
OTHER_FAILURES = [
    pytest.param(b"permission denied", DockerException, id="command-failure"),
    pytest.param(NOT_A_MANAGER, NotASwarmManager, id="not-a-swarm-manager"),
    pytest.param(
        b'context "resource": context is in use', DockerException, id="context-in-use"
    ),
    pytest.param(
        b"Error response from daemon: No such container: resource",
        NoSuchContainer,
        id="container-not-found",
    ),
]


def _context() -> tuple[DockerClient, Context]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    return client, Context(client.client_config, "resource", is_immutable_id=True)


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_context_cleanup_propagates_removal_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, context = _context()
    response = CompletedProcess([], 17, b"output", stderr)
    with (
        patch("subprocess.run", return_value=response) as command,
        pytest.raises(DockerException) as raised,
        context,
    ):
        pass
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "context",
        "remove",
        "--force",
        "resource",
    ]
    assert type(raised.value) is error_type
    assert raised.value.return_code == 17
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_context_cleanup_failure_joins_the_body_error(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, context = _context()
    body = RuntimeError("body")
    response = CompletedProcess([], 17, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(BaseExceptionGroup) as raised,
        context,
    ):
        raise body
    assert raised.value.message == "Context body and cleanup both failed"
    assert raised.value.exceptions[0] is body
    assert type(raised.value.exceptions[1]) is error_type


def _exists(client: DockerClient, context: Context, *, as_handle: bool) -> bool:
    return context.exists() if as_handle else client.context.exists("resource")


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_context_exists_is_true_after_inspect(as_handle: bool) -> None:
    client, context = _context()
    response = CompletedProcess([], 0, CONTEXT_JSON, b"")
    with patch("subprocess.run", return_value=response) as command:
        found = _exists(client, context, as_handle=as_handle)
    assert found is True
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "context",
        "inspect",
        "resource",
    ]


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(
    "stderr",
    [CONTEXT_MISSING, b'context "resource" does not exist'],
    ids=["inspect", "remove"],
)
def test_context_exists_is_false_for_a_missing_context(
    as_handle: bool, stderr: bytes
) -> None:
    client, context = _context()
    response = CompletedProcess([], 1, b"", stderr)
    with patch("subprocess.run", return_value=response):
        found = _exists(client, context, as_handle=as_handle)
    assert found is False


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_context_exists_propagates_other_failures(
    as_handle: bool, stderr: bytes, error_type: type[DockerException]
) -> None:
    client, context = _context()
    response = CompletedProcess([], 1, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(DockerException) as raised,
    ):
        _exists(client, context, as_handle=as_handle)
    assert type(raised.value) is error_type


def test_context_removed_inside_its_context_exits_cleanly(
    docker_client: DockerClient,
) -> None:
    with docker_client.context.create(
        "pywhales-removed-in-context", docker={"host": "ssh://test@test.domain"}
    ) as new_context:
        assert new_context.exists()
        assert docker_client.context.exists("pywhales-removed-in-context")
        new_context.remove(force=True)
        assert not new_context.exists()
    assert not new_context.exists()
    assert not docker_client.context.exists("pywhales-removed-in-context")
