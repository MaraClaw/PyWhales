import os
import random
import struct
from collections import Counter
from collections.abc import Callable, Sequence
from unittest.mock import patch

import pytest

from pywhales._launch_limits import (
    LaunchMeasurement,
    LaunchPlanner,
    argv_size,
    check_launch,
)
from pywhales.exceptions import ObservationLimitError
from pywhales.inventory import InspectionPolicy


def policy(limit: int) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=limit, max_response_bytes=1
    )


def test_caller_argv_limit_includes_utf8_and_terminators() -> None:
    args = ["command", "é"]
    assert argv_size(args) == 11
    check_launch(args, {}, policy(11))
    with pytest.raises(ObservationLimitError) as failure:
        check_launch(args, {}, policy(10))
    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "argv",
        10,
        11,
    )


def test_native_total_includes_environment_and_pointers() -> None:
    args, env = ["cmd"], {"KEY": "secret"}
    total = 4 + 11 + 4 * struct.calcsize("P")
    with patch("pywhales._launch_limits.os.sysconf", return_value=total):
        check_launch(args, env, policy(100))
    with (
        patch("pywhales._launch_limits.os.sysconf", return_value=total - 1),
        pytest.raises(ObservationLimitError) as failure,
    ):
        check_launch(args, env, policy(100))
    assert failure.value.measured == total
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "args,env", [(["secret\0arg"], {}), (["cmd"], {"key": "secret\0value"})]
)
def test_nul_is_rejected_without_exposing_values(
    args: list[str], env: dict[str, str]
) -> None:
    with pytest.raises(ObservationLimitError) as failure:
        check_launch(args, env, policy(100))
    assert "secret" not in repr(failure.value)


def test_unknown_native_limit_fails_closed() -> None:
    with (
        patch("pywhales._launch_limits.os.sysconf", side_effect=ValueError("secret")),
        pytest.raises(ObservationLimitError) as failure,
    ):
        check_launch(["cmd"], {}, policy(100))
    assert failure.value.reason == "native_limit_unknown"
    assert failure.value.__cause__ is None


def test_linux_per_string_limit_counts_environment_terminator() -> None:
    with (
        patch("pywhales._launch_limits.sys.platform", "linux"),
        patch(
            "pywhales._launch_limits.os.sysconf",
            side_effect=lambda name: 100000 if name == "SC_ARG_MAX" else 4,
        ),
    ):
        check_launch(["cmd"], {"K": "x" * 125}, policy(100))
        with pytest.raises(ObservationLimitError) as failure:
            check_launch(["cmd"], {"K": "x" * 126}, policy(100))
    assert (failure.value.reason, failure.value.measured) == ("native_string", 129)


def test_macos_does_not_apply_linux_string_limit() -> None:
    with (
        patch("pywhales._launch_limits.sys.platform", "darwin"),
        patch("pywhales._launch_limits.os.sysconf", return_value=100000),
    ):
        check_launch(["cmd", "x" * 1000], {}, policy(2000))


def test_windows_uses_quoted_utf16_command_and_environment() -> None:
    with patch("pywhales._launch_limits.os.name", "nt"):
        assert argv_size(["cmd", "a b", "😀"]) == 26
        check_launch(["cmd", "a b", "😀"], {"K": "V"}, policy(26))
        with pytest.raises(ObservationLimitError):
            check_launch(["cmd"], {"K": "x" * 32765}, policy(100))


def test_windows_command_native_boundary_in_utf16_units() -> None:
    with patch("pywhales._launch_limits.os.name", "nt"):
        check_launch(["x" * 32766], {}, policy(65536))
        with pytest.raises(ObservationLimitError) as failure:
            check_launch(["x" * 32767], {}, policy(65536))
    assert (failure.value.limit, failure.value.measured) == (32767, 32768)


def test_native_argv_size_matches_filesystem_encoding() -> None:
    assert argv_size(["é"]) == len(os.fsencode("é")) + 1


PLATFORMS = {
    "linux": ("posix", "linux"),
    "darwin": ("posix", "darwin"),
    "nt": ("nt", "win32"),
}
ALPHABET = 'abcxyz =\t"\\é😀\udc80'
EXERCISED = {
    "linux": {
        "accepted",
        "nul",
        "encoding",
        "argv",
        "environment",
        "native_limit_unknown",
        "native_string",
        "native_total",
    },
    "darwin": {
        "accepted",
        "nul",
        "encoding",
        "argv",
        "environment",
        "native_limit_unknown",
        "native_total",
    },
    "nt": {
        "accepted",
        "nul",
        "argv",
        "environment",
        "native_argv",
        "native_environment",
    },
}


def generated(rng: random.Random, longest: int = 40) -> str:
    # Rare long strings cross the Windows 32767-unit limits alone or in pairs.
    roll = rng.random()
    if roll < 0.02:
        length = rng.randint(32800, 33000)
    elif roll < 0.05:
        length = rng.randint(16500, 16600)
    else:
        length = rng.randint(0, longest)
    characters = rng.choices(ALPHABET, k=length)
    for special in ("\0", "\ud800"):
        if rng.random() < 0.03:
            characters.insert(rng.randint(0, len(characters)), special)
    return "".join(characters)


def generated_environment(rng: random.Random) -> dict[str, str]:
    environment = {}
    for _ in range(rng.randint(0, 5)):
        key = generated(rng, 8)
        if rng.random() < 0.03:
            key += "="
        environment[key] = generated(rng)
    return environment


def generated_policy(rng: random.Random) -> InspectionPolicy:
    return policy(rng.choice([rng.randint(1, 300), rng.randint(300, 3000), 10**7]))


def generated_sysconf(rng: random.Random) -> Callable[[str], int]:
    roll = rng.random()
    native_limit = rng.choice([rng.randint(1, 600), rng.randint(600, 3000), 10**7])
    page_size = rng.choice([1, 2, 4, 4096])
    if roll < 0.04:
        native_limit = 0
    elif roll < 0.08:
        page_size = -1

    def sysconf(name: str) -> int:
        if roll > 0.96:
            raise (OSError if roll > 0.98 else ValueError)(name)
        return native_limit if name == "SC_ARG_MAX" else page_size

    return sysconf


def outcome(check: Callable[..., None], *args: object) -> tuple[object, ...]:
    try:
        check(*args)
    except ObservationLimitError as error:
        return (error.reason, error.limit, error.measured)
    except UnicodeEncodeError as error:
        return ("encoding", error.args)
    return ("accepted",)


def greedy_plan(
    prefix: Sequence[str],
    arguments: Sequence[str],
    env: dict[str, str],
    limits: InspectionPolicy,
    max_count: int,
    plan: list[slice],
) -> None:
    # The whole-argv plan inventory staging used before incremental measurement.
    for argument in arguments:
        check_launch([*prefix, argument], env, limits)
    chunks: list[list[int]] = []
    chunk: list[int] = []
    for index in range(len(arguments)):
        candidate: list[int] = [*chunk, index]
        if len(candidate) <= max_count:
            try:
                check_launch(
                    [*prefix, *(arguments[member] for member in candidate)],
                    env,
                    limits,
                )
            except ObservationLimitError:
                if not chunk:
                    raise
            else:
                chunk = candidate
                continue
        chunks.append(chunk)
        chunk = [index]
    if chunk:
        chunks.append(chunk)
    plan.extend(slice(chunk[0], chunk[-1] + 1) for chunk in chunks)


def measured_plan(
    measurement: LaunchMeasurement,
    arguments: Sequence[str],
    max_count: int,
    plan: list[slice],
) -> None:
    for part in measurement.chunks(arguments, max_count):
        plan.append(part)


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
def test_measurement_matches_check_launch_for_generated_prefixes(platform: str) -> None:
    rng = random.Random(f"launch-measurement-{platform}")
    os_name, sys_platform = PLATFORMS[platform]
    seen: Counter[object] = Counter()
    for _ in range(300):
        prefix = [generated(rng) for _ in range(rng.randint(0, 3))]
        arguments = [generated(rng) for _ in range(rng.randint(0, 12))]
        env = generated_environment(rng)
        limits = generated_policy(rng)
        with (
            patch("pywhales._launch_limits.os.name", os_name),
            patch("pywhales._launch_limits.sys.platform", sys_platform),
            patch("pywhales._launch_limits.os.sysconf", generated_sysconf(rng)),
        ):
            measurement = LaunchPlanner(limits).measure(prefix, env)
            for count in range(len(arguments) + 1):
                if count:
                    measurement.append(arguments[count - 1])
                expected = outcome(
                    check_launch, [*prefix, *arguments[:count]], env, limits
                )
                assert outcome(measurement.check) == expected
                seen[expected[0]] += 1
            measurement.reset()
            assert outcome(measurement.check) == outcome(
                check_launch, prefix, env, limits
            )
    assert set(seen) == EXERCISED[platform]


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
def test_measured_chunks_match_greedy_whole_argv_plan(platform: str) -> None:
    rng = random.Random(f"launch-chunks-{platform}")
    os_name, sys_platform = PLATFORMS[platform]
    split = refused = 0
    for _ in range(300):
        prefix = [generated(rng) for _ in range(rng.randint(1, 3))]
        arguments = [generated(rng) for _ in range(rng.randint(0, 12))]
        env = generated_environment(rng)
        limits = generated_policy(rng)
        max_count = rng.randint(1, 5)
        expected: list[slice] = []
        planned: list[slice] = []
        with (
            patch("pywhales._launch_limits.os.name", os_name),
            patch("pywhales._launch_limits.sys.platform", sys_platform),
            patch("pywhales._launch_limits.os.sysconf", generated_sysconf(rng)),
        ):
            measurement = LaunchPlanner(limits).measure(prefix, env)
            result = outcome(measured_plan, measurement, arguments, max_count, planned)
            assert result == outcome(
                greedy_plan, prefix, arguments, env, limits, max_count, expected
            )
            if result == ("accepted",):
                assert planned == expected
                split += any(
                    part.stop - part.start < max_count for part in planned[:-1]
                )
                continue
            refused += 1
            # Runs before the first argument that cannot launch alone are yielded.
            covered = sum(part.stop - part.start for part in planned)
            alone = [
                outcome(check_launch, [*prefix, argument], env, limits)
                for argument in arguments
            ]
            assert alone[covered] == result
            assert all(single == ("accepted",) for single in alone[:covered])
    assert split and refused


def test_planner_reads_native_limits_once_per_planning_call() -> None:
    with (
        patch("pywhales._launch_limits.os.name", "posix"),
        patch("pywhales._launch_limits.sys.platform", "linux"),
        patch(
            "pywhales._launch_limits.os.sysconf",
            side_effect=lambda name: 100000 if name == "SC_ARG_MAX" else 4096,
        ) as sysconf,
    ):
        planner = LaunchPlanner(policy(100000))
        for prefix in (["cmd"], ["other", "inspect"]):
            measurement = planner.measure(prefix, {"K": "V"})
            references = [f"ref-{index}" for index in range(20)]
            assert len(list(measurement.chunks(references, 3))) == 7
            measurement.check()
        assert [call.args for call in sysconf.call_args_list] == [
            ("SC_ARG_MAX",),
            ("SC_PAGESIZE",),
        ]
        LaunchPlanner(policy(100000)).measure(["cmd"], {}).check()
    assert sysconf.call_count == 4
