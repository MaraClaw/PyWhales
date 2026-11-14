"""Image history, search, tree listing and platform options through a fake CLI.

Every case runs a recording Python script as the Docker CLI, so no daemon is used.
"""

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from pywhales import DockerClient, Image
from pywhales.client_config import ParsingError
from pywhales.components.image.models import ImageHistoryLayer, ImageSearchResult
from pywhales.exceptions import DockerTimeout
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

HISTORY_FLAGS = ["--format", "{{json .}}", "--no-trunc", "--human=false"]
SEARCH_FLAGS = ["--format", "{{json .}}", "--no-trunc"]
ALPINE_ID = "sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf"
_SEARCH = "search"
_LIST = "list"


def _fixture_lines(category: str) -> str:
    return "".join(
        json.dumps(json.loads(path.read_text())) + "\n"
        for path in get_all_jsons(category)
    )


def _inspect_rules(*references: str) -> list[Rule]:
    """Answer an `image inspect` of these references, or of any one, in order."""
    snapshots = {
        reference: {"Id": f"sha256:{index:064x}", "RepoTags": [reference]}
        for index, reference in enumerate(references, start=1)
    }
    return [
        Rule(
            match=("image", "inspect", *references),
            stdout=json.dumps([*snapshots.values()]),
        ),
        *(
            Rule(match=("image", "inspect", reference), stdout=json.dumps([snapshot]))
            for reference, snapshot in snapshots.items()
        ),
    ]


def _alpine_handle(client: DockerClient) -> Image:
    return Image(client.client_config, ALPINE_ID, is_immutable_id=True)


@pytest.mark.parametrize("json_file", get_all_jsons("image_history"))
def test_history_fixture_parses(json_file: Path) -> None:
    layer = ImageHistoryLayer.model_validate(json.loads(json_file.read_text()))
    assert layer.size >= 0
    assert layer.created_at.tzinfo is not None


@pytest.mark.parametrize("json_file", get_all_jsons("image_search"))
def test_search_fixture_parses(json_file: Path) -> None:
    result = ImageSearchResult.model_validate(json.loads(json_file.read_text()))
    assert result.name
    assert result.star_count >= 0


def test_history_argv_and_typed_layers(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("image", "history"), stdout=_fixture_lines("image_history"))],
    )
    layers = client.image.history("alpine")
    assert calls(log) == [["image", "history", *HISTORY_FLAGS, "alpine"]]
    assert [layer.size for layer in layers] == [0, 9310208]
    assert [layer.id for layer in layers] == [ALPINE_ID, None]
    assert layers[0].created_by == 'CMD ["/bin/sh"]'
    assert layers[1].comment == "buildkit.dockerfile.v0"


def test_history_platform_argv_and_handle_forwarding(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("image", "history"), stdout=_fixture_lines("image_history"))],
    )
    assert len(client.image.history("alpine", platform="linux/arm64")) == 2
    assert len(_alpine_handle(client).history(platform="linux/arm64")) == 2
    assert calls(log) == [
        ["image", "history", *HISTORY_FLAGS, "--platform", "linux/arm64", "alpine"],
        ["image", "history", *HISTORY_FLAGS, "--platform", "linux/arm64", ALPINE_ID],
    ]


def test_history_accepts_an_image_handle(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.image.history(_alpine_handle(client)) == []
    assert calls(log) == [["image", "history", *HISTORY_FLAGS, ALPINE_ID]]


@pytest.mark.parametrize("timeout", [0, -1.0, float("inf"), True])
def test_history_and_search_validate_timeout_before_launch(
    tmp_path: Path, timeout: float
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.image.history("alpine", timeout=timeout)
    with pytest.raises(ValueError):
        client.image.search("alpine", timeout=timeout)
    assert calls(log) == []


@pytest.mark.parametrize("operation", ["history", "search"])
def test_history_and_search_deadline_reaps_the_cli(
    tmp_path: Path, operation: str
) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(sleep=60)])
    with pytest.raises(DockerTimeout):
        getattr(client.image, operation)("alpine", timeout=0.5)


def test_malformed_history_output_is_a_value_free_parsing_error(
    tmp_path: Path,
) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("image", "history"), stdout="secret-not-json\n")]
    )
    with pytest.raises(ParsingError) as raised:
        client.image.history("alpine")
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_history_rejects_records_missing_fields(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("image", "history"), stdout='{"ID": "x"}\n')]
    )
    with pytest.raises(ParsingError):
        client.image.history("alpine")


def test_search_argv_filters_limit_and_typed_results(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("search",), stdout=_fixture_lines("image_search"))]
    )
    results = client.image.search(
        "alpine",
        filters=[("is-official", "true"), ("stars", "100")],
        limit=3,
    )
    assert calls(log) == [
        [
            "search",
            *SEARCH_FLAGS,
            "--filter",
            "is-official=true",
            "--filter",
            "stars=100",
            "--limit",
            "3",
            "alpine",
        ]
    ]
    assert [result.name for result in results] == [
        "alpine",
        "alpine/git",
        "alpine/socat",
    ]
    assert [result.is_official for result in results] == [True, False, False]
    assert results[0].star_count == 11583


def test_search_defaults_add_no_filter_or_limit(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.image.search("alpine") == []
    assert client.image.search("alpine", filters=None, limit=None) == []
    assert calls(log) == [["search", *SEARCH_FLAGS, "alpine"]] * 2


def test_search_accepts_a_mapping_and_keeps_a_zero_limit(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.image.search("alpine", filters={"stars": "5"}, limit=0)
    assert calls(log) == [
        ["search", *SEARCH_FLAGS, "--filter", "stars=5", "--limit", "0", "alpine"]
    ]


def test_search_filters_are_validated_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    search = getattr(client.image, _SEARCH)
    for bad in ("is-official=true", [("stars",)], [("stars", 1)], b"stars=1"):
        with pytest.raises(TypeError):
            search("alpine", filters=bad)
    assert calls(log) == []


def test_malformed_search_output_is_a_parsing_error(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("search",), stdout='{"Name": "alpine"}\n')]
    )
    with pytest.raises(ParsingError):
        client.image.search("alpine")


def test_default_list_argv_is_unchanged(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("image", "list"), stdout="sha256:b\nsha256:b\n")]
    )
    images = client.image.list("alpine", [("reference", "alp*")], True)
    assert [str(image) for image in images] == ["sha256:b"]
    assert calls(log) == [
        [
            "image",
            "list",
            "--quiet",
            "--no-trunc",
            "--all",
            "--filter",
            "reference=alp*",
            "alpine",
        ]
    ]


def test_tree_list_returns_the_raw_text_without_handles(tmp_path: Path) -> None:
    text = "IMAGE   ID\nalpine:latest   f59abe656b69"
    client, log = make_fake_cli(tmp_path, [Rule(match=("--tree",), stdout=text + "\n")])
    result = client.image.list("alpine", [("reference", "alp*")], True, tree=True)
    assert result == text
    assert client.images(tree=True) == text
    assert calls(log) == [
        [
            "image",
            "list",
            "--tree",
            "--all",
            "--filter",
            "reference=alp*",
            "alpine",
        ],
        ["image", "list", "--tree"],
    ]


def test_tree_list_rejects_hydration_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.image.list(hydrate=True, tree=True)
    assert calls(log) == []


def test_list_rejects_a_mapping_filter_in_tree_mode_before_launch(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    list_images = getattr(client.image, _LIST)
    with pytest.raises(TypeError):
        list_images(filters={"reference": "alpine"}, tree=True)
    assert calls(log) == []


def test_pull_without_new_options_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _inspect_rules("alpine"))
    client.image.pull("alpine", quiet=True, platform="linux/arm64")
    assert calls(log)[0] == [
        "image",
        "pull",
        "--quiet",
        "--platform=linux/arm64",
        "alpine",
    ]


def test_pull_all_tags_returns_none_after_one_repository_pull(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.image.pull("repo/app", all_tags=True) is None
    assert (
        client.image.pull(
            "localhost:5000/app", quiet=True, platform="linux/arm64", all_tags=True
        )
        is None
    )
    assert calls(log) == [
        ["image", "pull", "--all-tags", "repo/app"],
        [
            "image",
            "pull",
            "--quiet",
            "--platform=linux/arm64",
            "--all-tags",
            "localhost:5000/app",
        ],
    ]


def test_pull_all_tags_streams_progress_for_the_repository(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("image", "pull"), stdout="layer\n", stderr="warn\n")]
    )
    with client.image.pull("repo/app", stream_logs=True, all_tags=True) as stream:
        records = set(stream)
    assert records == {("repo/app", b"layer\n"), ("repo/app", b"warn\n")}
    assert calls(log) == [["image", "pull", "--all-tags", "repo/app"]]


_NOT_A_BARE_REPOSITORY: list[Callable[[], str | Iterable[str]]] = [
    list,
    lambda: ["zzz"],
    lambda: iter(["zzz"]),
    lambda: "zzz:tag",
    lambda: "localhost:5000/zzz:tag",
    lambda: "zzz@sha256:" + "0" * 64,
    lambda: "zzz:tag@sha256:" + "0" * 64,
]


@pytest.mark.parametrize("make_reference", _NOT_A_BARE_REPOSITORY)
def test_all_tags_requires_one_bare_repository_before_any_lookup(
    tmp_path: Path, make_reference: Callable[[], str | Iterable[str]]
) -> None:
    client, log = make_fake_cli(tmp_path)
    reference = make_reference()
    with pytest.raises(ValueError) as pulled:
        client.image.pull(reference, all_tags=True)
    with pytest.raises(ValueError) as streamed:
        client.image.pull(reference, stream_logs=True, all_tags=True)
    with pytest.raises(ValueError) as pushed:
        client.image.push(reference, all_tags=True)
    assert calls(log) == []
    for error in (pulled, streamed, pushed):
        assert "zzz" not in str(error.value)


def test_all_tags_keeps_the_stream_and_quiet_rule(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.image.pull("repo", quiet=True, stream_logs=True, all_tags=True)
    with pytest.raises(ValueError):
        client.image.push("repo", quiet=True, stream_logs=True, all_tags=True)
    assert calls(log) == []


def test_push_all_tags_skips_the_local_inspection(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.image.push("repo/app", all_tags=True) is None
    assert (
        client.image.push("repo/app", quiet=True, platform="linux/arm64", all_tags=True)
        is None
    )
    assert calls(log) == [
        ["image", "push", "--all-tags", "repo/app"],
        [
            "image",
            "push",
            "--quiet",
            "--platform",
            "linux/arm64",
            "--all-tags",
            "repo/app",
        ],
    ]


def test_push_all_tags_streams_progress(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("image", "push"), stdout="pushed\n")]
    )
    with client.image.push("repo/app", stream_logs=True, all_tags=True) as stream:
        assert list(stream) == [("repo/app", b"pushed\n")]


def test_push_without_new_options_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _inspect_rules("alpine:3"))
    client.image.push("alpine:3", quiet=True)
    assert calls(log) == [
        ["image", "inspect", "alpine:3"],
        ["image", "push", "--quiet", "alpine:3"],
    ]


def test_push_platform_applies_to_one_and_to_many_references(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _inspect_rules("one:1", "two:2"))
    client.image.push("one:1", platform="linux/arm64")
    client.image.push(["one:1", "two:2"], platform="linux/arm64")
    stream = client.image.push(
        ["one:1", "two:2"], stream_logs=True, platform="linux/arm64"
    )
    assert stream is not None
    with stream:
        list(stream)
    pushes = [argv for argv in calls(log) if argv[:2] == ["image", "push"]]
    assert sorted(pushes) == sorted(
        [
            ["image", "push", "--platform", "linux/arm64", name]
            for name in ("one:1",) * 3
        ]
        + [["image", "push", "--platform", "linux/arm64", "two:2"]] * 2
    )


def test_inspect_without_platform_still_returns_handles(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _inspect_rules("alpine:latest"))
    single = client.image.inspect("alpine:latest")
    batch = client.image.inspect(["alpine:latest"])
    assert isinstance(single, Image)
    assert [type(image) for image in batch] == [Image]
    assert calls(log) == [["image", "inspect", "alpine:latest"]] * 2


def test_inspect_with_platform_returns_typed_snapshots(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _inspect_rules("one:1", "two:2"))
    single = client.image.inspect("one:1", platform="linux/arm64")
    batch = client.image.inspect(iter(["one:1", "two:2"]), platform="linux/arm64")
    assert single.id == f"sha256:{1:064x}"
    assert [snapshot.id for snapshot in batch] == [
        f"sha256:{1:064x}",
        f"sha256:{2:064x}",
    ]
    assert calls(log) == [
        ["image", "inspect", "--platform", "linux/arm64", "one:1"],
        ["image", "inspect", "--platform", "linux/arm64", "one:1", "two:2"],
    ]


def test_inspect_with_platform_skips_the_cli_for_an_empty_request(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.image.inspect([], platform="linux/arm64") == []
    assert calls(log) == []


def test_inspect_with_platform_rejects_a_snapshot_for_another_reference(
    tmp_path: Path,
) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("image", "inspect"),
                stdout=json.dumps(
                    [{"Id": "sha256:" + "1" * 64, "RepoTags": ["other:1"]}]
                ),
            )
        ],
    )
    with pytest.raises(ParsingError) as raised:
        client.image.inspect("wanted:1", platform="linux/arm64")
    assert "identity does not match" in str(raised.value)


def test_load_path_forwards_each_platform(tmp_path: Path) -> None:
    archive = tmp_path / "images.tar"
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("image", "load"), stdout="Loaded image: alpine:3\n")]
    )
    loaded = client.image.load(
        archive, quiet=True, platform=["linux/amd64", "linux/arm64/v8"]
    )
    assert loaded == ["alpine:3"]
    assert calls(log) == [
        [
            "image",
            "load",
            "--input",
            str(archive),
            "--quiet",
            "--platform",
            "linux/amd64",
            "--platform",
            "linux/arm64/v8",
        ]
    ]


def test_load_bytes_forwards_a_platform_string(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("image", "load"),
                stdout="Loaded image: alpine:3\n",
                read_stdin=True,
            )
        ],
    )
    assert client.image.load(b"archive", platform="linux/amd64,linux/arm64") == [
        "alpine:3"
    ]
    assert calls(log) == [["image", "load", "--platform", "linux/amd64,linux/arm64"]]


def test_load_without_platform_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.image.load(tmp_path / "images.tar", quiet=True)
    assert calls(log) == [
        ["image", "load", "--input", str(tmp_path / "images.tar"), "--quiet"]
    ]


def test_load_validates_timeout_before_the_platform_or_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.image.load(b"archive", timeout=0, platform="linux/amd64")
    assert calls(log) == []


def test_save_to_a_path_forwards_platforms_after_inspection(tmp_path: Path) -> None:
    archive = tmp_path / "alpine.tar"
    client, log = make_fake_cli(tmp_path, _inspect_rules("alpine"))
    assert (
        client.image.save(["alpine"], archive, platform=["linux/arm64", "linux/amd64"])
        is None
    )
    assert calls(log) == [
        ["image", "inspect", "alpine"],
        [
            "image",
            "save",
            "--output",
            str(archive),
            "--platform",
            "linux/arm64",
            "--platform",
            "linux/amd64",
            "alpine",
        ],
    ]


def test_lazy_save_forwards_the_platform_on_first_iteration(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [*_inspect_rules("alpine"), Rule(match=("image", "save"), stdout="tar-bytes")],
    )
    archive = client.image.save("alpine", platform="linux/arm64")
    assert calls(log) == [["image", "inspect", "alpine"]]
    assert b"".join(archive) == b"tar-bytes"
    assert calls(log)[1] == [
        "image",
        "save",
        "--platform",
        "linux/arm64",
        "alpine",
    ]


def test_image_handle_forwards_platform_to_save_and_remove(tmp_path: Path) -> None:
    archive = tmp_path / "alpine.tar"
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("image", "inspect"), stdout=json.dumps([{"Id": ALPINE_ID}]))],
    )
    image = _alpine_handle(client)
    image.save(archive, platform="linux/arm64")
    image.remove(platform=["linux/amd64"])
    assert calls(log)[1:] == [
        [
            "image",
            "save",
            "--output",
            str(archive),
            "--platform",
            "linux/arm64",
            ALPINE_ID,
        ],
        ["image", "rm", "--platform", "linux/amd64", ALPINE_ID],
    ]


def test_remove_forwards_platforms_after_the_existing_flags(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.image.remove("alpine", True, False, platform=["linux/amd64", "linux/386"])
    client.image.remove("alpine")
    assert calls(log) == [
        [
            "image",
            "rm",
            "--force",
            "--no-prune",
            "--platform",
            "linux/amd64",
            "--platform",
            "linux/386",
            "alpine",
        ],
        ["image", "rm", "alpine"],
    ]


def test_remove_of_nothing_still_skips_the_cli(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.image.remove([], platform="linux/amd64")
    assert calls(log) == []
