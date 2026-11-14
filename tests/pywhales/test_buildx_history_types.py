"""Static contracts for Buildx history, policy, replay and imagetools returns.

The ty check analyzes these _check_* bodies without executing their Docker calls.
Pytest does not collect the functions; they are type-checker contracts, not
executable examples.
"""

from pathlib import Path
from typing import assert_type

from pydantic import JsonValue

from pywhales import DockerClient
from pywhales.components.buildx.history import BuildxHistoryCLI
from pywhales.components.buildx.history_models import BuildRecord, BuildRecordDetail
from pywhales.components.buildx.imagetools.models import Manifest
from pywhales.components.buildx.policy import BuildxPolicyCLI
from pywhales.components.buildx.replay import BuildxReplayCLI
from pywhales.utils import MappedProcessStream, ProcessStream


def _check_history_reads(history: BuildxHistoryCLI, debug: bool) -> None:
    assert_type(history.list(), list[BuildRecord])
    assert_type(
        history.list(filters={"status": "error"}, local=True, builder="b"),
        list[BuildRecord],
    )
    assert_type(history.list(filters=[("status", "error")]), list[BuildRecord])
    assert_type(history.list(debug=False), list[BuildRecord])
    assert_type(history.list(debug=True), str)
    assert_type(history.list(debug=debug), list[BuildRecord] | str)
    assert_type(history.inspect(), BuildRecordDetail)
    assert_type(history.inspect("abc", builder="b"), BuildRecordDetail)
    assert_type(history.inspect("abc", debug=True), str)
    assert_type(history.inspect("abc", debug=debug), BuildRecordDetail | str)
    assert_type(history.inspect_attachment("abc", "sha256:d", type="t"), str)
    assert_type(history.logs("abc", progress="rawjson", timeout=5), str)


def _check_history_actions(history: BuildxHistoryCLI, bundle: Path) -> None:
    assert_type(history.export(["a", "b"], finalize=True, output=bundle), None)
    assert_type(history.export(all=True, output="h.dockerbuild"), None)
    assert_type(history.import_([bundle, "b.dockerbuild"], timeout=5), None)
    assert_type(history.import_(bundle), None)
    assert_type(history.open("abc", timeout=5), None)
    assert_type(history.remove(["a"], builder="b"), None)
    assert_type(history.remove(all=True), None)
    assert_type(history.trace("abc", addr="127.0.0.1:0", timeout=5), ProcessStream)


def _check_history_models(record: BuildRecord, detail: BuildRecordDetail) -> None:
    assert_type(record.ref, str)
    assert_type(record.id, str)
    assert_type(detail.ref, str)
    assert_type(detail.duration, int | None)


def _check_policy(policy: BuildxPolicyCLI) -> None:
    assert_type(policy.eval("docker-image://alpine"), str)
    assert_type(
        policy.eval("src", file="Dockerfile", fields=["image"], print=True, timeout=5),
        str,
    )
    assert_type(policy.test("."), str)
    assert_type(policy.test(Path("p"), filename="f", run="r", timeout=5), str)


def _check_replay(replay: BuildxReplayCLI, dry_run: bool, stream: bool) -> None:
    assert_type(replay.build("alpine"), None)
    assert_type(replay.build("alpine", load=True, tags=["t"]), None)
    assert_type(replay.build("alpine", dry_run=True), dict[str, JsonValue])
    assert_type(replay.build("alpine", stream_logs=True), MappedProcessStream[str])
    assert_type(replay.build("alpine", dry_run=False, stream_logs=False), None)
    assert_type(
        replay.build("alpine", dry_run=dry_run, stream_logs=stream),
        dict[str, JsonValue] | MappedProcessStream[str] | None,
    )


def _check_imagetools(client: DockerClient, debug: bool, dry_run: bool) -> None:
    imagetools = client.buildx.imagetools
    assert_type(imagetools.inspect("alpine"), Manifest)
    assert_type(imagetools.inspect("alpine", builder="b"), Manifest)
    assert_type(imagetools.inspect("alpine", debug=True), str)
    assert_type(imagetools.inspect("alpine", debug=debug), Manifest | str)
    assert_type(imagetools.create(["alpine"], prefer_index=False), None)
    assert_type(imagetools.create(["alpine"], dry_run=True, progress="plain"), Manifest)
    assert_type(
        imagetools.create(
            ["alpine"],
            platforms=["linux/arm64"],
            metadata_file=Path("m.json"),
            debug=True,
        ),
        None,
    )
    assert_type(imagetools.create(["alpine"], dry_run=dry_run), Manifest | None)
