"""Argv and parsing tests for the Compose v5.7.1 commands, options and globals.

A recording fake CLI stands in for Docker, so nothing here touches a daemon or a
Compose project. Saved listings in `jsons/compose_*` come from a real Compose
v5.7.1 run; one-line records are stored one per file and joined into the
newline-delimited stream the CLI prints.
"""

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import Parameter
from pathlib import Path
from time import monotonic
from typing import Literal

import pytest

from pywhales import DockerClient
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components.compose.bridge import (
    ComposeBridgeCLI,
    ComposeTransformationsCLI,
)
from pywhales.components.compose.cli_wrapper import ComposeCLI, ComposeRunArgs
from pywhales.components.compose.models import (
    ComposeImage,
    ComposeStats,
    ComposeVolume,
)
from pywhales.exceptions import DockerException, DockerTimeout
from pywhales.test_utils import get_all_jsons
from pywhales.utils import ProcessStream
from tests.pywhales.components.argv_fakes import (
    Rule,
    calls,
    fake_cli_call,
    make_fake_cli,
)

FULL_ID = "sha256:" + "ab12" * 16
FAILING = Rule(match=("compose",), exit_code=1, stderr="no containers for project\n")


def _json_lines(category: str) -> str:
    """Join one saved record per file into the CLI's newline-delimited output."""
    return "\n".join(path.read_text().strip() for path in get_all_jsons(category))


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------


def test_new_globals_default_to_unset() -> None:
    config = ClientConfig()

    assert config.compose_all_resources is False
    assert config.compose_ansi is None


@pytest.mark.parametrize("mode", ["never", "always", "auto"])
def test_globals_follow_progress_and_precede_the_subcommand(
    tmp_path: Path, mode: Literal["never", "always", "auto"]
) -> None:
    client_call, log = fake_cli_call(tmp_path)
    config = ClientConfig(
        client_call=client_call,
        compose_project_name="demo",
        compose_progress="plain",
        compose_all_resources=True,
        compose_ansi=mode,
    )

    DockerClient(client_config=config).compose.version()

    assert calls(log) == [
        [
            "compose",
            "--project-name",
            "demo",
            "--progress",
            "plain",
            "--all-resources",
            "--ansi",
            mode,
            "version",
        ]
    ]


def test_client_keywords_set_the_new_globals(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(tmp_path)
    client = DockerClient(
        client_call=client_call, compose_all_resources=True, compose_ansi="never"
    )

    client.compose.version()

    assert calls(log) == [["compose", "--all-resources", "--ansi", "never", "version"]]


def test_default_globals_add_nothing_to_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.version()

    assert calls(log) == [["compose", "version"]]


def test_globals_take_part_in_config_equality() -> None:
    assert ClientConfig() == ClientConfig()
    assert ClientConfig(compose_all_resources=True) != ClientConfig()
    assert ClientConfig(compose_ansi="never") != ClientConfig()
    assert ClientConfig(compose_ansi="never") != ClientConfig(compose_ansi="always")


# ---------------------------------------------------------------------------
# New commands
# ---------------------------------------------------------------------------


def test_attach_forwards_every_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.attach(
        "app", detach_keys="ctrl-x", index=2, stdin=False, sig_proxy=False
    )
    client.compose.attach("app")

    assert calls(log) == [
        [
            "compose",
            "attach",
            "--detach-keys",
            "ctrl-x",
            "--index",
            "2",
            "--no-stdin",
            "--sig-proxy=false",
            "app",
        ],
        ["compose", "attach", "--sig-proxy", "app"],
    ]


def test_attach_deadline_kills_the_local_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("attach",), sleep=30)])
    started = monotonic()

    with pytest.raises(DockerTimeout):
        client.compose.attach("app", stdin=False, timeout=0.5)

    assert monotonic() - started < 20
    assert calls(log) == [["compose", "attach", "--no-stdin", "--sig-proxy", "app"]]


def test_commit_returns_the_image_named_in_the_progress_text(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("commit",), stderr=f" demo-app-1 Committed as {FULL_ID}\n")],
    )

    image = client.compose.commit(
        "app",
        "repo:v2",
        author="Ann <ann@example.com>",
        changes=["ENV A=1", "CMD x"],
        index=2,
        message="snapshot",
        pause=False,
    )

    assert image.id == FULL_ID
    assert image.client_config is client.client_config
    assert calls(log) == [
        [
            "compose",
            "--progress",
            "plain",
            "commit",
            "--author",
            "Ann <ann@example.com>",
            "--change",
            "ENV A=1",
            "--change",
            "CMD x",
            "--index",
            "2",
            "--message",
            "snapshot",
            "--pause=false",
            "app",
            "repo:v2",
        ]
    ]


def test_commit_keeps_the_default_pause_and_leaves_the_image_untagged(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("commit",), stderr=f"Committed as {FULL_ID}\n")]
    )

    client.compose.commit("app", changes="ENV A=1")

    assert calls(log) == [
        [
            "compose",
            "--progress",
            "plain",
            "commit",
            "--change",
            "ENV A=1",
            "app",
        ]
    ]


def test_commit_ignores_the_client_progress_mode(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(
        tmp_path, [Rule(match=("commit",), stderr=f"Committed as {FULL_ID}\n")]
    )
    config = ClientConfig(client_call=client_call, compose_progress="quiet")

    DockerClient(client_config=config).compose.commit("app")

    assert calls(log)[0][:3] == ["compose", "--progress", "plain"]
    assert config.compose_progress == "quiet"


@pytest.mark.parametrize(
    "stderr", ["", " demo-app-1 Committed\n", "Committed as sha256:abc...\n"]
)
def test_commit_without_a_full_id_is_a_value_free_parsing_error(
    tmp_path: Path, stderr: str
) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("commit",), stderr=stderr)])

    with pytest.raises(ParsingError) as error:
        client.compose.commit("app")

    assert "Committed" not in str(error.value)


def test_commit_is_rejected_in_dry_run_before_any_launch(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(tmp_path)
    client = DockerClient(client_call=client_call, compose_dry_run=True)

    with pytest.raises(ValueError, match=r"dry.run"):
        client.compose.commit("app")

    assert calls(log) == []


def test_commit_validates_the_deadline_before_any_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError, match="timeout"):
        client.compose.commit("app", timeout=0)

    assert calls(log) == []


def test_commit_surfaces_a_failing_service(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("commit",),
                exit_code=1,
                stderr='service "app" is not running\n',
            )
        ],
    )

    with pytest.raises(DockerException):
        client.compose.commit("app")


def test_cp_forwards_endpoints_and_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.cp(
        ("app", "/etc/hosts"),
        tmp_path / "hosts",
        all=True,
        archive=True,
        follow_link=True,
        index=2,
    )
    client.compose.cp("local.txt", ("app", Path("/data/local.txt")))

    assert calls(log) == [
        [
            "compose",
            "cp",
            "--all",
            "--archive",
            "--follow-link",
            "--index",
            "2",
            "app:/etc/hosts",
            str(tmp_path / "hosts"),
        ],
        ["compose", "cp", "local.txt", "app:/data/local.txt"],
    ]


def test_export_writes_to_the_output_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.export("app", tmp_path / "app.tar", index=2)
    client.compose.export("job", "job.tar")

    assert calls(log) == [
        [
            "compose",
            "export",
            "--index",
            "2",
            "--output",
            str(tmp_path / "app.tar"),
            "app",
        ],
        ["compose", "export", "--output", "job.tar", "job"],
    ]


@pytest.mark.parametrize("method", ["attach", "cp", "export", "scale", "wait", "stats"])
def test_new_deadlines_reject_bad_values_before_any_launch(
    tmp_path: Path, method: str
) -> None:
    client, log = make_fake_cli(tmp_path)
    arguments: dict[str, tuple[object, ...]] = {
        "attach": ("app",),
        "cp": ("a.txt", ("app", "/a.txt")),
        "export": ("app", "app.tar"),
        "scale": ({},),
        "wait": ("app",),
        "stats": (),
    }

    for bad in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="timeout"):
            getattr(client.compose, method)(*arguments[method], timeout=bad)

    assert calls(log) == []


def test_images_lists_one_entry_per_container(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("images",), stdout="[]")])

    client.compose.images()
    client.compose.images([])
    client.compose.images("app")
    client.compose.images(["app", "job"])

    assert calls(log) == [
        ["compose", "images", "--format", "json"],
        ["compose", "images", "--format", "json"],
        ["compose", "images", "--format", "json", "app"],
        ["compose", "images", "--format", "json", "app", "job"],
    ]


@pytest.mark.parametrize("path", get_all_jsons("compose_images"), ids=lambda p: p.stem)
def test_images_parse_saved_output(tmp_path: Path, path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("images",), stdout=path.read_text())]
    )

    result = client.compose.images()

    expected = {"0": 3, "1": 0}[path.stem]
    assert len(result) == expected
    for entry in result:
        assert isinstance(entry, ComposeImage)
        assert entry.id == (
            "sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf"
        )
        assert (entry.repository, entry.tag) == ("alpine", "latest")
        assert entry.platform == "linux/arm64/v8"
        assert entry.size == 13502455
        assert entry.created == datetime(2026, 11, 5, 18, 0, 31, 536531, tzinfo=UTC)
        assert entry.last_tag_time is not None
        assert entry.last_tag_time.year == 2026
    assert [entry.container_name for entry in result] == [
        "pyw130-h-1-app-1",
        "pyw130-h-1-app-2",
        "pyw130-h-1-job-1",
    ][:expected]


@pytest.mark.parametrize("stdout", ["{not json", '{"ID": "x"}', '[{"Size": "big"}]'])
def test_images_reject_malformed_output_without_the_payload(
    tmp_path: Path, stdout: str
) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("images",), stdout=stdout)])

    with pytest.raises(ParsingError) as error:
        client.compose.images()

    assert error.value.__cause__ is None
    assert "big" not in str(error.value)


def test_scale_passes_counts_after_the_no_deps_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.scale({"app": 2, "job": 0}, no_deps=True)
    client.compose.scale({"app": 1})

    assert calls(log) == [
        ["compose", "scale", "--no-deps", "app=2", "job=0"],
        ["compose", "scale", "app=1"],
    ]


def test_scale_without_services_is_a_no_op(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    assert client.compose.scale({}) is None
    assert client.compose.scale({}, no_deps=True, timeout=5) is None

    assert calls(log) == []


def test_stats_takes_an_untruncated_snapshot(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    assert client.compose.stats() == []
    client.compose.stats("app", all=True, aggregate=True, all_projects=True)

    assert calls(log) == [
        ["compose", "stats", "--no-stream", "--no-trunc", "--format", "json"],
        [
            "compose",
            "stats",
            "--all",
            "--aggregate",
            "--all-projects",
            "--no-stream",
            "--no-trunc",
            "--format",
            "json",
            "app",
        ],
    ]


def test_stats_parse_saved_records_of_every_shape(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("stats",), stdout=_json_lines("compose_stats") + "\n")]
    )

    per_container, per_project_row, aggregate = client.compose.stats(all=True)

    assert isinstance(per_container, ComposeStats)
    assert per_container.name == "pyw130-h-1-app-1"
    assert per_container.container == per_container.id
    assert per_container.pids == "0"
    assert per_container.cpu_perc == "0.00%"
    assert per_container.mem_usage == "0B / 0B"
    assert per_container.project is None
    assert per_project_row.project == "pyw130-h-1"
    assert per_project_row.name == "pyw130-h-1-app-1"
    assert per_project_row.pids == 0
    assert per_project_row.container is None
    assert aggregate.project == "pyw130-h-1"
    assert aggregate.containers == 3
    assert aggregate.name is None


def test_stats_reject_malformed_lines_without_the_payload(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("stats",), stdout='{"Containers": "secret"}\n')]
    )

    with pytest.raises(ParsingError) as error:
        client.compose.stats()

    assert error.value.__cause__ is None
    assert "secret" not in str(error.value)


def test_stats_stream_returns_the_raw_owned_stream(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("stats",), stdout="frame\n")])

    with client.compose.stats("app", stream=True, all=True) as stream:
        assert isinstance(stream, ProcessStream)
        records = list(stream)

    assert records == [("stdout", b"frame\n")]
    assert calls(log) == [["compose", "stats", "--all", "app"]]


def test_volumes_lists_project_volumes(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    assert client.compose.volumes() == []
    client.compose.volumes([])
    client.compose.volumes("app")
    client.compose.volumes(["app", "job"])

    assert calls(log) == [
        ["compose", "volumes", "--format", "json"],
        ["compose", "volumes", "--format", "json"],
        ["compose", "volumes", "--format", "json", "app"],
        ["compose", "volumes", "--format", "json", "app", "job"],
    ]


def test_volumes_parse_saved_records(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [Rule(match=("volumes",), stdout=_json_lines("compose_volumes") + "\n")],
    )

    (volume,) = client.compose.volumes()

    assert isinstance(volume, ComposeVolume)
    assert volume.name == "pyw130-h-1_data"
    assert volume.driver == "local"
    assert volume.scope == "local"
    assert volume.size == "N/A"
    assert volume.mountpoint == "/var/lib/docker/volumes/pyw130-h-1_data/_data"
    assert volume.labels is not None
    assert "com.docker.compose.project=pyw130-h-1" in volume.labels.split(",")


def test_volumes_reject_malformed_lines_without_the_payload(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("volumes",), stdout="not-json-secret\n")]
    )

    with pytest.raises(ParsingError) as error:
        client.compose.volumes()

    assert error.value.__cause__ is None
    assert "secret" not in str(error.value)


def test_wait_returns_the_container_exit_status(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("wait",),
                stdout='container "abc" exited with status code 3\n',
                exit_code=3,
            )
        ],
    )

    assert client.compose.wait("job", down_project=True) == 3
    assert client.compose.wait(["job", "app"]) == 3

    assert calls(log) == [
        ["compose", "wait", "--down-project", "job"],
        ["compose", "wait", "job", "app"],
    ]


def test_wait_returns_zero_for_a_clean_stop(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [Rule(match=("wait",), stdout='container "abc" exited with status code 0\n')],
    )

    assert client.compose.wait("job") == 0


@pytest.mark.parametrize("exit_code", [1, 3])
def test_wait_raises_when_the_cli_reports_on_stderr(
    tmp_path: Path, exit_code: int
) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("wait",),
                exit_code=exit_code,
                stderr='no containers for project "demo"\n',
            )
        ],
    )

    with pytest.raises(DockerException) as error:
        client.compose.wait("ghost")

    assert error.value.return_code == exit_code


def test_wait_deadline_is_not_mistaken_for_an_exit_status(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("wait",), sleep=30)])
    started = monotonic()

    with pytest.raises(DockerTimeout):
        client.compose.wait("app", timeout=0.5)

    assert monotonic() - started < 20


@pytest.mark.parametrize("services", [[], ()])
def test_wait_rejects_an_empty_selection_before_any_launch(
    tmp_path: Path, services: list[str] | tuple[str, ...]
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError, match="service"):
        client.compose.wait(services)

    assert calls(log) == []


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


def test_bridge_is_wired_to_the_shared_config() -> None:
    client = DockerClient()

    assert isinstance(client.compose.bridge, ComposeBridgeCLI)
    assert isinstance(client.compose.bridge.transformations, ComposeTransformationsCLI)
    assert client.compose.bridge.client_config is client.client_config
    assert client.compose.bridge.transformations.client_config is client.client_config


def test_bridge_convert_forwards_every_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.bridge.convert()
    client.compose.bridge.convert(
        output=tmp_path / "out",
        templates="tpl",
        transformations=["one:1", "two:2"],
        yes=True,
    )
    client.compose.bridge.convert(transformations="solo:1")

    assert calls(log) == [
        ["compose", "bridge", "convert"],
        [
            "compose",
            "bridge",
            "convert",
            "--output",
            str(tmp_path / "out"),
            "--templates",
            "tpl",
            "--transformation",
            "one:1",
            "--transformation",
            "two:2",
            "--yes",
        ],
        ["compose", "bridge", "convert", "--transformation", "solo:1"],
    ]


def test_bridge_calls_carry_the_compose_globals(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(tmp_path)
    config = ClientConfig(client_call=client_call, compose_project_name="demo")

    DockerClient(client_config=config).compose.bridge.convert()

    assert calls(log) == [["compose", "--project-name", "demo", "bridge", "convert"]]


def test_transformation_list_parses_image_summaries(tmp_path: Path) -> None:
    summary = (
        '[{"Containers":-1,"Created":1700000000,"Id":"sha256:aa","Labels":'
        '{"com.docker.compose.bridge":"transformation"},"ParentId":"",'
        '"Descriptor":{"size":7},"RepoDigests":[],"RepoTags":["demo:t"],'
        '"SharedSize":-1,"Size":42}]'
    )
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("transformations",), stdout=summary)]
    )

    (transformation,) = client.compose.bridge.transformations.list()

    assert calls(log) == [
        ["compose", "bridge", "transformations", "list", "--format", "json"]
    ]
    assert transformation.id == "sha256:aa"
    assert transformation.repo_tags == ["demo:t"]
    assert transformation.labels == {"com.docker.compose.bridge": "transformation"}
    assert transformation.created == datetime.fromtimestamp(1700000000, tz=UTC)
    assert transformation.size == 42
    assert transformation.descriptor == {"size": 7}


@pytest.mark.parametrize("stdout", ["[]", "null"])
def test_transformation_list_without_images_is_empty(
    tmp_path: Path, stdout: str
) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("transformations",), stdout=stdout)]
    )

    assert client.compose.bridge.transformations.list() == []


def test_transformation_list_rejects_other_shapes(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("transformations",), stdout='{"Id": "x"}')]
    )

    with pytest.raises(ParsingError):
        client.compose.bridge.transformations.list()


def test_transformation_create_copies_a_source_into_a_directory(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.bridge.transformations.create(tmp_path / "mine", from_="base:1")
    client.compose.bridge.transformations.create("other")

    assert calls(log) == [
        [
            "compose",
            "bridge",
            "transformations",
            "create",
            "--from",
            "base:1",
            str(tmp_path / "mine"),
        ],
        ["compose", "bridge", "transformations", "create", "other"],
    ]


@pytest.mark.parametrize("bad", [0, -1, True, float("nan")])
def test_bridge_deadlines_reject_bad_values_before_any_launch(
    tmp_path: Path, bad: float
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError, match="timeout"):
        client.compose.bridge.convert(timeout=bad)
    with pytest.raises(ValueError, match="timeout"):
        client.compose.bridge.transformations.create("dir", timeout=bad)

    assert calls(log) == []


# ---------------------------------------------------------------------------
# Config queries and modifiers
# ---------------------------------------------------------------------------


def test_config_modifiers_follow_the_json_format(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("config",), stdout="{}")])

    assert (
        client.compose.config(
            True,
            no_interpolate=True,
            no_normalize=True,
            no_path_resolution=True,
            no_consistency=True,
            no_env_resolution=True,
            lock_image_digests=True,
            resolve_image_digests=True,
        )
        == {}
    )

    assert calls(log) == [
        [
            "compose",
            "config",
            "--format",
            "json",
            "--no-interpolate",
            "--no-normalize",
            "--no-path-resolution",
            "--no-consistency",
            "--no-env-resolution",
            "--lock-image-digests",
            "--resolve-image-digests",
        ]
    ]


def test_config_quiet_and_output_read_nothing_back(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("config",), stdout="garbage")])

    assert client.compose.config(quiet=True) is None
    assert client.compose.config(output=tmp_path / "out.yaml") is None
    assert client.compose.config(True, output="out.json", quiet=True) is None

    assert calls(log) == [
        ["compose", "config", "--format", "json", "--quiet"],
        [
            "compose",
            "config",
            "--format",
            "json",
            "--output",
            str(tmp_path / "out.yaml"),
        ],
        [
            "compose",
            "config",
            "--format",
            "json",
            "--output",
            "out.json",
            "--quiet",
        ],
    ]


@pytest.mark.parametrize(
    "kind", ["services", "volumes", "profiles", "images", "networks", "models"]
)
def test_config_names_lists_one_name_per_line(tmp_path: Path, kind: str) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("config",), stdout="app\njob\napp\n")]
    )

    method = "config_names"

    names = getattr(client.compose, method)(kind)

    assert names == ["app", "job", "app"]
    assert calls(log) == [["compose", "config", f"--{kind}"]]


def test_config_names_of_an_empty_listing_is_empty(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path)

    assert client.compose.config_names("profiles") == []


@pytest.mark.parametrize("kind", ["", "service", "services --hash", "--hash", "format"])
def test_config_names_rejects_unknown_kinds_before_any_launch(
    tmp_path: Path, kind: str
) -> None:
    client, log = make_fake_cli(tmp_path)
    method = "config_names"

    with pytest.raises(ValueError, match="kind"):
        getattr(client.compose, method)(kind)

    assert calls(log) == []


def test_config_hashes_selects_services_in_one_comma_list(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("config",), stdout="app abc123\njob def456\n")]
    )

    assert client.compose.config_hashes() == {"app": "abc123", "job": "def456"}
    client.compose.config_hashes([])
    client.compose.config_hashes("app")
    client.compose.config_hashes(["app", "job"])

    assert calls(log) == [
        ["compose", "config", "--hash", "*"],
        ["compose", "config", "--hash", "*"],
        ["compose", "config", "--hash", "app"],
        ["compose", "config", "--hash", "app,job"],
    ]


@pytest.mark.parametrize("stdout", ["garbage\n", "app\n", " abc\n"])
def test_config_hashes_reject_lines_that_are_not_a_pair(
    tmp_path: Path, stdout: str
) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("config",), stdout=stdout)])

    with pytest.raises(ParsingError) as error:
        client.compose.config_hashes()

    assert "garbage" not in str(error.value)


def test_config_variables_lists_every_reference(tmp_path: Path) -> None:
    stdout = (
        '{"TAG":{"Name":"TAG","DefaultValue":"latest","PresenceValue":"",'
        '"Required":false},"PORT":{"Name":"PORT","DefaultValue":"",'
        '"PresenceValue":"","Required":true}}'
    )
    client, log = make_fake_cli(tmp_path, [Rule(match=("config",), stdout=stdout)])

    first, second = client.compose.config_variables()

    assert calls(log) == [["compose", "config", "--variables", "--format", "json"]]
    assert (first.name, first.default_value, first.required) == ("TAG", "latest", False)
    assert (second.name, second.required) == ("PORT", True)


def test_config_variables_without_references_is_empty(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("config",), stdout="{}")])

    assert client.compose.config_variables() == []


@pytest.mark.parametrize("stdout", ["[]", "not json", '{"A": 1}'])
def test_config_variables_reject_other_shapes(tmp_path: Path, stdout: str) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("config",), stdout=stdout)])

    with pytest.raises(ParsingError):
        client.compose.config_variables()


def test_config_environment_splits_on_the_first_separator(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("config",), stdout="A=1\nB=x=y\nC=\n")]
    )

    assert client.compose.config_environment() == {"A": "1", "B": "x=y", "C": ""}
    assert calls(log) == [["compose", "config", "--environment"]]


def test_config_environment_rejects_continuation_lines(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("config",), stdout="A=first\nsecond-line\n")]
    )

    with pytest.raises(ParsingError) as error:
        client.compose.config_environment()

    assert "second-line" not in str(error.value)


# ---------------------------------------------------------------------------
# Options on existing commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("memory", ["512m", 1024])
def test_build_forwards_the_memory_limit(tmp_path: Path, memory: int | str) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.build("app", memory=memory)

    assert calls(log) == [["compose", "build", "--memory", str(memory), "app"]]


def test_create_forwards_scales_and_prompt_answers(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.create(
        "app",
        quiet_pull=True,
        remove_orphans=True,
        scales={"app": 2, "job": 1},
        yes=True,
    )

    assert calls(log) == [
        [
            "compose",
            "create",
            "--quiet-pull",
            "--remove-orphans",
            "--scale",
            "app=2",
            "--scale",
            "job=1",
            "--yes",
            "app",
        ]
    ]


def test_events_forwards_the_time_window(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    with client.compose.events(["app"], since="1h", until="now") as stream:
        assert list(stream) == []

    assert calls(log) == [
        [
            "compose",
            "events",
            "--json",
            "--since",
            "1h",
            "--until",
            "now",
            "app",
        ]
    ]


def test_execute_emits_the_canonical_no_tty_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.execute("app", ["ls"], tty=False)

    (argv,) = calls(log)
    assert "--no-tty" in argv
    assert "--no-TTY" not in argv


def test_run_emits_the_canonical_no_tty_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.run("app", ["ls"], tty=False)

    assert calls(log) == [["compose", "run", "--no-tty", "app", "ls"]]


def test_kill_forwards_remove_orphans(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.kill("app", remove_orphans=True)

    assert calls(log) == [["compose", "kill", "--remove-orphans", "app"]]


def test_logs_forwards_the_replica_index(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    assert client.compose.logs("app", index=2) == ""

    assert calls(log) == [["compose", "logs", "--no-color", "--index", "2", "app"]]


def test_ps_forwards_filters_statuses_and_orphans(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ps",), stdout="id1\nid2\n")])

    containers = client.compose.ps(
        ["app"],
        True,
        filters={"status": "running"},
        status=["running", "exited"],
        orphans=False,
    )
    client.compose.ps(filters=[("status", "paused")], status="paused", orphans=True)

    assert [container.id for container in containers] == ["id1", "id2"]
    assert calls(log) == [
        [
            "compose",
            "ps",
            "--quiet",
            "--all",
            "--filter",
            "status=running",
            "--status",
            "running",
            "--status",
            "exited",
            "--orphans=false",
            "app",
        ],
        [
            "compose",
            "ps",
            "--quiet",
            "--filter",
            "status=paused",
            "--status",
            "paused",
            "--orphans",
        ],
    ]


def test_ps_can_return_service_names(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ps",), stdout="app\njob\n")])

    names = client.compose.ps(["app"], True, service_names=True)

    assert names == ["app", "job"]
    assert calls(log) == [["compose", "ps", "--services", "--all", "app"]]


def test_ps_service_names_of_an_empty_project_are_empty(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path)

    assert client.compose.ps(service_names=True) == []


def test_ps_rejects_more_than_one_filter_before_any_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError, match="filter"):
        client.compose.ps(filters={"status": "running", "name": "app"})

    assert calls(log) == []


@pytest.mark.parametrize("service_names", [False, True])
def test_ps_is_rejected_in_dry_run_before_any_launch(
    tmp_path: Path, service_names: bool
) -> None:
    client_call, log = fake_cli_call(tmp_path)
    client = DockerClient(client_call=client_call, compose_dry_run=True)

    with pytest.raises(ValueError, match=r"dry.run"):
        client.compose.ps(service_names=service_names)

    assert calls(log) == []


def test_publish_forwards_the_app_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.publish("registry/demo:1", app=True)

    assert calls(log) == [["compose", "publish", "--app", "registry/demo:1"]]


def test_pull_forwards_buildable_skips_and_policy(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.pull("app", ignore_buildable=True, policy="always")

    assert calls(log) == [
        ["compose", "pull", "--ignore-buildable", "--policy", "always", "app"]
    ]


def test_push_forwards_every_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.push(
        ["app"], ignore_missing=True, include_deps=True, quiet=True, timeout=30
    )

    assert calls(log) == [
        [
            "compose",
            "push",
            "--ignore-missing",
            "--include-deps",
            "--quiet",
            "app",
        ]
    ]


def test_restart_forwards_no_deps(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.restart("app", no_deps=True)

    assert calls(log) == [["compose", "restart", "--no-deps", "app"]]


def test_run_forwards_every_new_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.run(
        "app",
        ["echo", "hi"],
        tty=False,
        cap_add=["NET_ADMIN", "SYS_TIME"],
        cap_drop=["MKNOD"],
        env_from_files=["a.env", Path("b.env")],
        interactive=False,
        no_recreate=True,
        pull="always",
        quiet=True,
        quiet_build=True,
        quiet_pull=True,
        remove_orphans=True,
    )

    assert calls(log) == [
        [
            "compose",
            "run",
            "--no-tty",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "SYS_TIME",
            "--cap-drop",
            "MKNOD",
            "--env-from-file",
            "a.env",
            "--env-from-file",
            "b.env",
            "--interactive=false",
            "--no-recreate",
            "--pull",
            "always",
            "--quiet",
            "--quiet-build",
            "--quiet-pull",
            "--remove-orphans",
            "app",
            "echo",
            "hi",
        ]
    ]


@pytest.mark.parametrize(
    ("interactive", "expected"),
    [(None, []), (True, ["--interactive"]), (False, ["--interactive=false"])],
)
def test_run_interactive_is_tristate(
    tmp_path: Path, interactive: bool | None, expected: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.run("app", tty=False, interactive=interactive)

    assert calls(log) == [["compose", "run", "--no-tty", *expected, "app"]]


def test_run_takes_one_env_file_without_a_list(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.run("app", tty=False, env_from_files=Path("only.env"))

    assert calls(log) == [
        ["compose", "run", "--no-tty", "--env-from-file", "only.env", "app"]
    ]


def test_run_arguments_typed_dict_covers_the_client_alias_keywords() -> None:
    parameters = set(inspect.signature(ComposeCLI.run).parameters)

    assert parameters - {"self", "service", "command", "detach", "stream"} == set(
        ComposeRunArgs.__annotations__
    )


def test_start_forwards_the_wait_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.start("app", wait=True, wait_timeout=30)

    assert calls(log) == [["compose", "start", "--wait", "--wait-timeout", "30", "app"]]


def test_up_forwards_every_new_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.up(
        ["app"],
        always_recreate_deps=True,
        attach_services=["app", "job"],
        menu=False,
        quiet_build=True,
        quiet_pull=True,
        shutdown_timeout=timedelta(seconds=10, milliseconds=900),
        timestamps=True,
    )

    assert calls(log) == [
        [
            "compose",
            "up",
            "--always-recreate-deps",
            "--menu=false",
            "--quiet-build",
            "--quiet-pull",
            "--timeout",
            "10",
            "--timestamps",
            "--attach",
            "app",
            "--attach",
            "job",
            "app",
        ]
    ]


@pytest.mark.parametrize(
    ("menu", "expected"),
    [(None, []), (True, ["--menu"]), (False, ["--menu=false"])],
)
def test_up_menu_is_tristate(
    tmp_path: Path, menu: bool | None, expected: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.up(menu=menu)

    assert calls(log) == [["compose", "up", *expected]]


@pytest.mark.parametrize("shutdown_timeout", [0, 15, timedelta(seconds=15)])
def test_up_shutdown_timeout_keeps_zero_and_whole_seconds(
    tmp_path: Path, shutdown_timeout: int | timedelta
) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.up(shutdown_timeout=shutdown_timeout)

    expected = "0" if shutdown_timeout == 0 else "15"
    assert calls(log) == [["compose", "up", "--timeout", expected]]


def test_up_single_attach_service_is_one_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.compose.up(attach_services="app")

    assert calls(log) == [["compose", "up", "--attach", "app"]]


@pytest.mark.parametrize(("short", "expected"), [(False, "version"), (True, "--short")])
def test_version_can_ask_for_the_short_form(
    tmp_path: Path, short: bool, expected: str
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("version",), stdout="5.7.1\n")])

    assert client.compose.version(short=short) == "5.7.1"

    assert calls(log)[0][-1] == expected


# ---------------------------------------------------------------------------
# Signature shape
# ---------------------------------------------------------------------------

KEYWORD_ONLY: list[tuple[Callable[..., object], list[str]]] = [
    (ComposeCLI.attach, ["detach_keys", "index", "stdin", "sig_proxy", "timeout"]),
    (ComposeCLI.build, ["memory"]),
    (
        ComposeCLI.config,
        [
            "no_interpolate",
            "no_normalize",
            "no_path_resolution",
            "no_consistency",
            "no_env_resolution",
            "lock_image_digests",
            "resolve_image_digests",
            "output",
            "quiet",
        ],
    ),
    (ComposeCLI.create, ["quiet_pull", "remove_orphans", "scales", "yes"]),
    (ComposeCLI.events, ["since", "until", "timeout"]),
    (ComposeCLI.kill, ["remove_orphans"]),
    (ComposeCLI.logs, ["index", "timeout"]),
    (ComposeCLI.ps, ["filters", "status", "orphans", "service_names"]),
    (ComposeCLI.publish, ["app"]),
    (ComposeCLI.pull, ["ignore_buildable", "policy", "timeout"]),
    (ComposeCLI.push, ["ignore_missing", "include_deps", "quiet", "timeout"]),
    (ComposeCLI.restart, ["no_deps"]),
    (
        ComposeCLI.run,
        [
            "cap_add",
            "cap_drop",
            "env_from_files",
            "interactive",
            "no_recreate",
            "pull",
            "quiet",
            "quiet_build",
            "quiet_pull",
            "remove_orphans",
            "timeout",
        ],
    ),
    (ComposeCLI.start, ["wait", "wait_timeout"]),
    (
        ComposeCLI.up,
        [
            "always_recreate_deps",
            "attach_services",
            "menu",
            "quiet_build",
            "quiet_pull",
            "shutdown_timeout",
            "timestamps",
        ],
    ),
    (ComposeCLI.version, ["short"]),
    (ComposeCLI.commit, ["author", "changes", "index", "message", "pause", "timeout"]),
    (ComposeCLI.cp, ["all", "archive", "follow_link", "index", "timeout"]),
    (ComposeCLI.export, ["index", "timeout"]),
    (ComposeCLI.scale, ["no_deps", "timeout"]),
    (
        ComposeCLI.stats,
        ["all", "aggregate", "all_projects", "stream", "timeout"],
    ),
    (ComposeCLI.wait, ["down_project", "timeout"]),
    (
        ComposeBridgeCLI.convert,
        ["output", "templates", "transformations", "yes", "timeout"],
    ),
    (ComposeTransformationsCLI.create, ["from_", "timeout"]),
]


@pytest.mark.parametrize(
    ("method", "names"),
    KEYWORD_ONLY,
    ids=lambda value: getattr(value, "__qualname__", ""),
)
def test_new_options_are_keyword_only(
    method: Callable[..., object], names: list[str]
) -> None:
    parameters = inspect.signature(method).parameters

    for name in names:
        assert parameters[name].kind is Parameter.KEYWORD_ONLY, name


def test_up_gains_no_second_prompt_option() -> None:
    parameters = inspect.signature(ComposeCLI.up).parameters

    assert "yes" not in parameters
    assert (
        parameters["use_default_sensitive_values"].kind
        is Parameter.POSITIONAL_OR_KEYWORD
    )


@pytest.mark.parametrize(
    "name",
    [
        "attach",
        "commit",
        "cp",
        "export",
        "images",
        "scale",
        "stats",
        "volumes",
        "wait",
        "config_names",
        "config_hashes",
        "config_variables",
        "config_environment",
    ],
)
def test_new_commands_are_reachable_from_the_client(name: str) -> None:
    assert callable(getattr(DockerClient().compose, name))
