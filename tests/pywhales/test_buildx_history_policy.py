"""Buildx history, policy and replay commands asserted as exact argv.

The fake CLI never launches Docker. Record and detail payloads come from the real
Buildx v0.39.0 CLI: the listing lines below and the numbered files under
`jsons/buildx_history/`.
"""

import json
from pathlib import Path

import pytest

from pywhales import DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.buildx.history import BuildxHistoryCLI
from pywhales.components.buildx.history_models import BuildRecordDetail
from pywhales.components.buildx.policy import BuildxPolicyCLI
from pywhales.components.buildx.replay import BuildxReplayCLI
from pywhales.exceptions import DockerException, DockerTimeout
from pywhales.test_utils import get_all_jsons
from pywhales.utils import MappedProcessStream, ProcessStream
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

LISTING = "\n".join(
    [
        '{"cached_steps":1,"completed_at":"2026-11-14T11:53:49.818180379Z","completed_steps":5,"created_at":"2026-11-14T11:53:49.655662796Z","name":"ctx3","ref":"default/default/m7spfgxr2ex1fods4bv9ydtn8","status":"Error","total_steps":5}',
        '{"cached_steps":1,"completed_at":"2026-11-14T11:53:49.397079212Z","completed_steps":7,"created_at":"2026-11-14T11:53:48.935528379Z","name":"ctx2 (final)","ref":"default/default/daltj5mlj48xvyn9sho7w9z3i","status":"Completed","total_steps":7}',
    ]
)
PLAN = {"subject": {"name": "alpine"}, "steps": [1, 2]}


def history(client: DockerClient) -> BuildxHistoryCLI:
    return BuildxHistoryCLI(client.client_config)


def policy(client: DockerClient) -> BuildxPolicyCLI:
    return BuildxPolicyCLI(client.client_config)


def replay(client: DockerClient) -> BuildxReplayCLI:
    return BuildxReplayCLI(client.client_config)


def fixture_text(number: int) -> str:
    return get_all_jsons("buildx_history")[number].read_text()


def test_list_emits_json_format_by_default(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert history(client).list() == []
    assert calls(log) == [["buildx", "history", "ls", "--format", "json"]]


def test_list_forwards_every_option_in_order(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    history(client).list(
        filters=[("status", "error"), ("status", "canceled")],
        local=True,
        builder="b1",
    )
    history(client).list(filters={"status": "error"})
    assert calls(log) == [
        [
            "buildx",
            "history",
            "ls",
            "--builder",
            "b1",
            "--filter",
            "status=error",
            "--filter",
            "status=canceled",
            "--local",
            "--format",
            "json",
        ],
        [
            "buildx",
            "history",
            "ls",
            "--filter",
            "status=error",
            "--format",
            "json",
        ],
    ]


def test_list_parses_one_record_per_line(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("history", "ls"), stdout=LISTING + "\n")]
    )
    failed, completed = history(client).list()
    assert failed.ref == "default/default/m7spfgxr2ex1fods4bv9ydtn8"
    assert failed.id == "m7spfgxr2ex1fods4bv9ydtn8"
    assert (failed.name, failed.status) == ("ctx3", "Error")
    assert (failed.completed_steps, failed.total_steps, failed.cached_steps) == (
        5,
        5,
        1,
    )
    assert failed.created_at is not None and failed.created_at.tzinfo is not None
    assert failed.completed_at is not None
    assert completed.name == "ctx2 (final)"
    assert completed.id == "daltj5mlj48xvyn9sho7w9z3i"


def test_list_debug_returns_human_text_and_the_debug_log(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("history", "ls"), stdout="BUILD ID\n", stderr="debug log\n")],
    )
    assert history(client).list(debug=True) == "BUILD ID\ndebug log"
    assert calls(log) == [["buildx", "history", "ls", "--debug"]]


@pytest.mark.parametrize("output", ["not json", "[1]", '{"name": "no ref"}'])
def test_list_malformed_output_raises_parsing_error_without_cause(
    tmp_path: Path, output: str
) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("history", "ls"), stdout=output)])
    with pytest.raises(ParsingError) as raised:
        history(client).list()
    assert raised.value.__cause__ is None


def test_inspect_requests_json_and_parses_the_record(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("history", "inspect"), stdout=fixture_text(0))]
    )
    detail = history(client).inspect("n05qf75ban22nlq56a2dgroyx", builder="b1")
    assert detail.ref == "n05qf75ban22nlq56a2dgroyx"
    assert calls(log) == [
        [
            "buildx",
            "history",
            "inspect",
            "--builder",
            "b1",
            "--format",
            "json",
            "n05qf75ban22nlq56a2dgroyx",
        ]
    ]


def test_inspect_without_ref_leaves_the_default_to_buildx(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("history", "inspect"), stdout=fixture_text(0))]
    )
    history(client).inspect()
    assert calls(log) == [["buildx", "history", "inspect", "--format", "json"]]


def test_inspect_debug_returns_human_text_and_the_debug_log(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("history", "inspect"), stdout="Name:\tctx\n", stderr="trace\n")],
    )
    assert history(client).inspect("abc", debug=True) == "Name:\tctx\ntrace"
    assert calls(log) == [["buildx", "history", "inspect", "--debug", "abc"]]


@pytest.mark.parametrize("output", ["", "[]", '{"Name": "no ref"}'])
def test_inspect_malformed_output_raises_parsing_error(
    tmp_path: Path, output: str
) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("history", "inspect"), stdout=output)]
    )
    with pytest.raises(ParsingError):
        history(client).inspect("abc")


@pytest.mark.parametrize("json_file", get_all_jsons("buildx_history"))
def test_detail_fixtures_parse(json_file: Path) -> None:
    detail = BuildRecordDetail.model_validate(json.loads(json_file.read_text()))
    assert detail.ref
    assert detail.status in {"completed", "failed"}
    assert detail.started_at is not None and detail.started_at.tzinfo is not None
    assert isinstance(detail.duration, int)


def test_detail_models_the_recorded_build_inputs_and_outputs() -> None:
    detail = BuildRecordDetail.model_validate(json.loads(fixture_text(1)))
    assert detail.target == "final"
    assert detail.platform == ["linux/arm64"]
    assert [(arg.name, arg.value) for arg in detail.build_args or []] == [
        ("GREETING", "hello")
    ]
    assert [(label.name, label.value) for label in detail.labels or []] == [
        ("pyw130.g2", "1")
    ]
    assert detail.config is not None
    assert (detail.config.image_resolve_mode, detail.config.no_cache) == ("local", True)
    assert (detail.num_completed_steps, detail.num_total_steps) == (7, 7)
    materials = detail.materials or []
    assert materials[0].uri is not None
    assert materials[0].uri.startswith("pkg:docker/alpine")
    assert (materials[0].digests or [""])[0].startswith("sha256:")
    attachments = detail.attachments or []
    assert [attachment.platform for attachment in attachments] == [
        None,
        None,
        "linux/arm64",
    ]
    assert attachments[1].type == "https://slsa.dev/provenance/v1"


def test_detail_models_a_failed_build_error() -> None:
    detail = BuildRecordDetail.model_validate(json.loads(fixture_text(2)))
    assert detail.status == "failed"
    assert detail.error is not None
    assert detail.error.code == 2
    assert detail.error.name == "[2/2] RUN exit 7"
    assert detail.error.message is not None
    assert "exit code: 7" in detail.error.message
    assert detail.error.sources is not None
    assert detail.error.stack is not None


def test_detail_maps_go_acronym_names_and_keeps_unknown_fields() -> None:
    detail = BuildRecordDetail.model_validate(
        {
            "Ref": "r",
            "VCSRepository": "git@example.test:team/app.git",
            "VCSRevision": "0123abc",
            "Config": {"Network": "none", "NoCache": False},
            "Future": {"nested": [1, "two"]},
        }
    )
    assert detail.vcs_repository == "git@example.test:team/app.git"
    assert detail.vcs_revision == "0123abc"
    assert detail.model_extra == {"Future": {"nested": [1, "two"]}}
    assert detail.config is not None
    assert detail.config.model_extra == {"Network": "none"}


def test_inspect_attachment_selects_by_type_and_platform(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("attachment",), stdout='{"schemaVersion": 2}\n')]
    )
    text = history(client).inspect_attachment(
        "abc",
        "sha256:d1",
        type="https://slsa.dev/provenance/v1",
        platform="linux/arm64",
        builder="b1",
        debug=True,
    )
    history(client).inspect_attachment()
    assert text == '{"schemaVersion": 2}'
    assert calls(log) == [
        [
            "buildx",
            "history",
            "inspect",
            "attachment",
            "--debug",
            "--builder",
            "b1",
            "--type",
            "https://slsa.dev/provenance/v1",
            "--platform",
            "linux/arm64",
            "abc",
            "sha256:d1",
        ],
        ["buildx", "history", "inspect", "attachment"],
    ]


def test_inspect_attachment_digest_requires_a_ref(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).inspect_attachment(digest="sha256:d1")
    assert calls(log) == []


def test_logs_return_the_stderr_stream(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("history", "logs"), stderr="#1 DONE 0.0s\n")],
    )
    text = history(client).logs(
        "abc", progress="plain", builder="b1", debug=True, timeout=30
    )
    history(client).logs()
    assert text == "#1 DONE 0.0s"
    assert calls(log) == [
        [
            "buildx",
            "history",
            "logs",
            "--debug",
            "--builder",
            "b1",
            "--progress",
            "plain",
            "abc",
        ],
        ["buildx", "history", "logs"],
    ]


def test_logs_validate_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).logs("abc", timeout=0)
    assert calls(log) == []


def test_logs_deadline_covers_the_local_cli(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("history", "logs"), sleep=30)])
    with pytest.raises(DockerTimeout):
        history(client).logs("abc", timeout=0.3)


def test_export_forwards_refs_and_the_output_path(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    bundle = tmp_path / "h.dockerbuild"
    history(client).export(
        ["a", "b"], finalize=True, output=bundle, builder="b1", debug=True, timeout=30
    )
    history(client).export("a", output=str(bundle))
    history(client).export(all=True, output=bundle)
    assert calls(log) == [
        [
            "buildx",
            "history",
            "export",
            "--debug",
            "--builder",
            "b1",
            "--finalize",
            "--output",
            str(bundle),
            "a",
            "b",
        ],
        ["buildx", "history", "export", "--output", str(bundle), "a"],
        ["buildx", "history", "export", "--all", "--output", str(bundle)],
    ]


def test_export_validates_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).export(["a"], all=True, output=tmp_path / "h")
    with pytest.raises(ValueError):
        history(client).export(output=tmp_path / "h", timeout=-1)
    assert calls(log) == []


def test_import_repeats_the_file_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert history(client).import_([tmp_path / "a.dockerbuild", "b.dockerbuild"]) == []
    history(client).import_("c.dockerbuild", builder="b1", debug=True, timeout=30)
    assert calls(log) == [
        [
            "buildx",
            "history",
            "import",
            "--file",
            str(tmp_path / "a.dockerbuild"),
            "--file",
            "b.dockerbuild",
        ],
        [
            "buildx",
            "history",
            "import",
            "--debug",
            "--builder",
            "b1",
            "--file",
            "c.dockerbuild",
        ],
    ]


def test_import_returns_the_dashboard_urls_from_stderr(tmp_path: Path) -> None:
    # Buildx v0.39.0 prints one URL per imported record on stderr; debug log lines
    # share the stream and are skipped.
    stderr = (
        'time="2026-11-14T13:50:00Z" level=debug msg="uploading bundle"\n'
        "docker-desktop://dashboard/build/.imported/_/fj87ohhpkjtq30f62q3sup2oz\n"
        "docker-desktop://dashboard/build/.imported/_/k60u07hj2c16uipgg3jou7hn2\n"
    )
    client, _ = make_fake_cli(tmp_path, [Rule(("import",), stderr=stderr)])
    assert history(client).import_("a.dockerbuild", debug=True) == [
        "docker-desktop://dashboard/build/.imported/_/fj87ohhpkjtq30f62q3sup2oz",
        "docker-desktop://dashboard/build/.imported/_/k60u07hj2c16uipgg3jou7hn2",
    ]


@pytest.mark.parametrize("files", [[], ()])
def test_import_rejects_an_empty_file_list_before_launch(
    tmp_path: Path, files: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).import_(files)
    with pytest.raises(ValueError):
        history(client).import_(["a"], timeout=0)
    assert calls(log) == []


def test_open_waits_for_the_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    history(client).open("abc", builder="b1", debug=True, timeout=30)
    history(client).open()
    assert calls(log) == [
        ["buildx", "history", "open", "--debug", "--builder", "b1", "abc"],
        ["buildx", "history", "open"],
    ]


def test_open_failure_raises_docker_exception(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [Rule(match=("history", "open"), stderr="no Docker Desktop\n", exit_code=1)],
    )
    with pytest.raises(DockerException) as raised:
        history(client).open("abc")
    assert raised.value.stderr == "no Docker Desktop\n"


def test_open_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).open(timeout=0)
    assert calls(log) == []


def test_remove_forwards_refs_or_all(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    history(client).remove(["a", "b"], builder="b1", debug=True)
    history(client).remove("a")
    history(client).remove(all=True)
    history(client).remove()
    assert calls(log) == [
        ["buildx", "history", "rm", "--debug", "--builder", "b1", "a", "b"],
        ["buildx", "history", "rm", "a"],
        ["buildx", "history", "rm", "--all"],
        ["buildx", "history", "rm"],
    ]


def test_remove_rejects_all_with_refs_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).remove(["a"], all=True)
    assert calls(log) == []


def test_trace_returns_an_owned_stream_of_labelled_lines(tmp_path: Path) -> None:
    url_line = "Trace available at http://127.0.0.1:1/trace/a...b\n"
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("history", "trace"), stdout='{"data": []}\n', stderr=url_line)],
    )
    stream = history(client).trace(
        "a", addr="127.0.0.1:0", compare="b", builder="b1", debug=True, timeout=30
    )
    assert isinstance(stream, ProcessStream)
    with stream:
        pairs = set(stream)
    assert pairs == {("stdout", b'{"data": []}\n'), ("stderr", url_line.encode())}
    assert calls(log) == [
        [
            "buildx",
            "history",
            "trace",
            "--debug",
            "--builder",
            "b1",
            "--addr",
            "127.0.0.1:0",
            "--compare",
            "b",
            "a",
        ]
    ]


def test_trace_without_options_passes_no_selection(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with history(client).trace() as stream:
        assert list(stream) == []
    assert calls(log) == [["buildx", "history", "trace"]]


def test_trace_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        history(client).trace(timeout=0)
    assert calls(log) == []


def test_trace_deadline_is_delivered_by_the_stream(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("history", "trace"), sleep=30)])
    with (
        pytest.raises(DockerTimeout),
        history(client).trace("a", timeout=0.3) as stream,
    ):
        list(stream)


def test_policy_eval_forwards_every_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("policy", "eval"), stdout='{"image": {}}\n')]
    )
    text = policy(client).eval(
        "docker-image://alpine",
        file=tmp_path / "Dockerfile",
        platform="linux/arm64",
        fields=["image", "env"],
        print=True,
        builder="b1",
        debug=True,
        timeout=30,
    )
    policy(client).eval("docker-image://alpine", fields="image")
    policy(client).eval("docker-image://alpine")
    assert text == '{"image": {}}'
    assert calls(log) == [
        [
            "buildx",
            "policy",
            "eval",
            "--debug",
            "--builder",
            "b1",
            "--fields",
            "image",
            "--fields",
            "env",
            "--file",
            str(tmp_path / "Dockerfile"),
            "--platform",
            "linux/arm64",
            "--print",
            "docker-image://alpine",
        ],
        ["buildx", "policy", "eval", "--fields", "image", "docker-image://alpine"],
        ["buildx", "policy", "eval", "docker-image://alpine"],
    ]


def test_policy_eval_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        policy(client).eval("docker-image://alpine", timeout=0)
    assert calls(log) == []


def test_policy_eval_denial_raises_docker_exception(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [Rule(match=("policy", "eval"), stderr="policy denied\n", exit_code=1)],
    )
    with pytest.raises(DockerException) as raised:
        policy(client).eval("docker-image://busybox")
    assert raised.value.return_code == 1


def test_policy_test_forwards_every_option(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("policy", "test"), stdout="test_a: PASS (allow=true)\n")],
    )
    text = policy(client).test(
        ".",
        filename="app.Dockerfile",
        run="alpine",
        builder="b1",
        debug=True,
        timeout=30,
    )
    policy(client).test(Path("policies"))
    assert text == "test_a: PASS (allow=true)"
    assert calls(log) == [
        [
            "buildx",
            "policy",
            "test",
            "--debug",
            "--builder",
            "b1",
            "--filename",
            "app.Dockerfile",
            "--run",
            "alpine",
            ".",
        ],
        ["buildx", "policy", "test", "policies"],
    ]


def test_policy_test_failure_keeps_the_report_on_the_exception(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("policy", "test"),
                stdout="test_b: FAIL (allow=false)\n",
                exit_code=1,
            )
        ],
    )
    with pytest.raises(DockerException) as raised:
        policy(client).test(".")
    assert raised.value.stdout == "test_b: FAIL (allow=false)\n"


def test_policy_test_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        policy(client).test(".", timeout=-1)
    assert calls(log) == []


def test_replay_build_blocks_and_returns_none(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    result = replay(client).build(
        "registry.example/app:1",
        load=True,
        network="none",
        outputs=[{"type": "local", "dest": "out"}, "type=tar,dest=app.tar"],
        platforms=["linux/amd64", "linux/arm64"],
        progress="plain",
        push=True,
        replay_mode="frontend",
        secrets="id=token,src=/run/token",
        ssh=["default", "key=/home/k"],
        tags=["app:replay", "app:two"],
        builder="b1",
        debug=True,
        timeout=30,
    )
    replay(client).build("registry.example/app:1")
    assert result is None
    assert calls(log) == [
        [
            "buildx",
            "replay",
            "build",
            "--debug",
            "--builder",
            "b1",
            "--load",
            "--network",
            "none",
            "--output",
            "type=local,dest=out",
            "--output",
            "type=tar,dest=app.tar",
            "--platform",
            "linux/amd64",
            "--platform",
            "linux/arm64",
            "--progress",
            "plain",
            "--push",
            "--replay-mode",
            "frontend",
            "--secret",
            "id=token,src=/run/token",
            "--ssh",
            "default",
            "--ssh",
            "key=/home/k",
            "--tag",
            "app:replay",
            "--tag",
            "app:two",
            "registry.example/app:1",
        ],
        ["buildx", "replay", "build", "registry.example/app:1"],
    ]


def test_replay_build_accepts_a_single_output_mapping(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    replay(client).build("alpine", outputs={"type": "local", "dest": tmp_path})
    assert calls(log) == [
        [
            "buildx",
            "replay",
            "build",
            "--output",
            f"type=local,dest={tmp_path}",
            "alpine",
        ]
    ]


def test_replay_dry_run_requests_json_and_returns_the_plan(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("replay", "build"), stdout=json.dumps(PLAN) + "\n")]
    )
    plan = replay(client).build("alpine", dry_run=True, platforms="linux/arm64")
    assert plan == PLAN
    assert calls(log) == [
        [
            "buildx",
            "replay",
            "build",
            "--dry-run",
            "--format",
            "json",
            "--platform",
            "linux/arm64",
            "alpine",
        ]
    ]


@pytest.mark.parametrize("output", ["", "not json", "[1]"])
def test_replay_dry_run_malformed_output_raises_parsing_error(
    tmp_path: Path, output: str
) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("replay", "build"), stdout=output)]
    )
    with pytest.raises(ParsingError) as raised:
        replay(client).build("alpine", dry_run=True)
    assert raised.value.__cause__ is None


def test_replay_stream_logs_returns_decoded_text(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("replay", "build"), stdout="out\n", stderr="err\n")],
    )
    stream = replay(client).build("alpine", stream_logs=True, timeout=30)
    assert isinstance(stream, MappedProcessStream)
    with stream:
        assert sorted(stream) == ["err\n", "out\n"]
    assert calls(log) == [["buildx", "replay", "build", "alpine"]]


def test_replay_rejects_dry_run_with_streaming_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        replay(client).build("alpine", dry_run=True, stream_logs=True)
    assert calls(log) == []


def test_replay_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        replay(client).build("alpine", timeout=0)
    assert calls(log) == []


def test_replay_deadline_covers_the_local_cli(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("replay", "build"), sleep=30)])
    with pytest.raises(DockerTimeout):
        replay(client).build("alpine", timeout=0.3)
