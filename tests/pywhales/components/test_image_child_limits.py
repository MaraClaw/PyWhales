"""Image transfer fan-out sized to the observation's remaining child slots.

Fake children reserve real observation child slots and rendezvous on barriers, so
peaks are proven by construction: no process starts and nothing sleeps.
"""

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from threading import Barrier, Event, Lock, get_ident
from typing import Literal, override
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._observation import _ChildReservation, get_observation, observation
from pywhales._scoped_launch import check_scoped_launch
from pywhales.components.image.cli_wrapper import ImageCLI
from pywhales.components.image.streams import MultiImageStream, map_in_contexts
from pywhales.exceptions import DockerException, ObservationLimitError
from pywhales.inventory import InspectionPolicy
from pywhales.utils import MappedProcessStream

BOUNDED_WAIT = 5.0
NAMES = [f"image-{index}" for index in range(5)]
MODES = pytest.mark.parametrize("mode", ["blocking", "stream"])

type Mode = Literal["blocking", "stream"]


def child_policy(children: int | None, attempts: int | None = None) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_launch_attempts=attempts,
        max_concurrent_children=children,
    )


class Launches:
    """Fake launches that reserve child slots and track how many live at once.

    The first ``len(names) // parties * parties`` names meet at a barrier while
    holding their slot, so ``parties`` launches must overlap for the run to pass.
    Any child waits for ``until_refused``, when set, before yielding its output.
    """

    def __init__(self, names: Sequence[str], parties: int) -> None:
        self.live = 0
        self.peak = 0
        self.threads: set[int] = set()
        self.until_refused: Event | None = None
        self._lock = Lock()
        self._meeting = Barrier(parties)
        self._gated = frozenset(names[: len(names) // parties * parties])

    def start(self, name: str) -> _ChildReservation | None:
        with self._lock:
            self.threads.add(get_ident())
        state = get_observation()
        try:
            reservation = (
                None if state is None else check_scoped_launch(["cli"], {}, state)
            )
        except ObservationLimitError:
            if self.until_refused is not None:
                self.until_refused.set()
            raise
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        return reservation

    def meet(self, name: str) -> None:
        if self.until_refused is not None:
            assert self.until_refused.wait(BOUNDED_WAIT)
        if name in self._gated:
            self._meeting.wait(BOUNDED_WAIT)

    def reap(self, reservation: _ChildReservation | None) -> None:
        with self._lock:
            self.live -= 1
        if reservation is not None:
            reservation.release()


class FakeChild(MappedProcessStream[tuple[str, bytes]]):
    """Child holding its slot from launch until exhaustion or ``close()``."""

    def __init__(self, launches: Launches, name: str) -> None:
        self._launches = launches
        self._name = name
        self._reservation = launches.start(name)
        self._yielded = False
        self._reaped = False

    @override
    def _on_failure(
        self, callback: Callable[[OSError | BufferError | DockerException], None]
    ) -> None:
        """Fake children have no process to fail."""

    @override
    def __next__(self) -> tuple[str, bytes]:
        if self._yielded:
            self.close()
            raise StopIteration
        self._yielded = True
        self._launches.meet(self._name)
        return self._name, b"progress"

    @override
    def close(self) -> None:
        if not self._reaped:
            self._reaped = True
            self._launches.reap(self._reservation)


def transfer(launches: Launches) -> Callable[[str], str]:
    def run_one(name: str) -> str:
        child = FakeChild(launches, name)
        try:
            for _ in child:
                pass
        finally:
            child.close()
        return name

    return run_one


def stream_group(names: Sequence[str], launches: Launches) -> MultiImageStream:
    return MultiImageStream(names, lambda name: FakeChild(launches, name))


def drain(stream: MultiImageStream) -> list[str]:
    with stream:
        return sorted(name for name, _ in stream)


def run(mode: Mode, names: Sequence[str], launches: Launches) -> list[str]:
    if mode == "blocking":
        return map_in_contexts(transfer(launches), names)
    return drain(stream_group(names, launches))


@MODES
def test_fan_out_fits_the_remaining_child_slots(mode: Mode) -> None:
    launches = Launches(NAMES, parties=2)
    with observation(child_policy(2)) as state:
        assert run(mode, NAMES, launches) == NAMES
    assert launches.peak == 2
    assert [account.consumed for account in state.child_accounts] == [0]


@MODES
def test_fan_out_counts_slots_already_used_in_outer_scopes(mode: Mode) -> None:
    launches = Launches(NAMES, parties=2)
    with observation(child_policy(3)) as outer:
        held = check_scoped_launch(["cli"], {}, outer)
        with observation(child_policy(4)) as inner:
            assert inner.child_allowance() == 2
            assert run(mode, NAMES, launches) == NAMES
        held.release()
    assert launches.peak == 2


@MODES
@pytest.mark.parametrize("scoped", [False, True], ids=["unobserved", "child-unbounded"])
def test_fan_out_stays_at_four_without_a_child_bound(mode: Mode, scoped: bool) -> None:
    names = [f"image-{index}" for index in range(6)]
    launches = Launches(names, parties=4)
    with ExitStack() as stack:
        if scoped:
            stack.enter_context(observation(child_policy(None, attempts=10)))
        assert run(mode, names, launches) == names
    assert launches.peak == 4


@MODES
def test_zero_allowance_starts_one_worker_and_refuses_without_a_debit(
    mode: Mode,
) -> None:
    launches = Launches(NAMES, parties=2)
    with observation(child_policy(1, attempts=5)) as state:
        held = check_scoped_launch(["cli"], {}, state)
        with pytest.raises(ObservationLimitError) as refused:
            run(mode, NAMES, launches)
        assert refused.value.reason == "concurrent_children"
        assert len(launches.threads) == 1
        assert launches.peak == 0
        assert [a.consumed for a in state.launch_accounts] == [1]
        assert [a.consumed for a in state.child_accounts] == [1]
        held.release()


def test_stream_sizes_in_its_creation_context_when_iteration_starts() -> None:
    launches = Launches(NAMES, parties=2)
    with observation(child_policy(3)) as state:
        stream = stream_group(NAMES, launches)
    held = state.reserve_child()
    assert drain(stream) == NAMES
    held.release()
    assert launches.peak == 2
    assert [account.consumed for account in state.child_accounts] == [0]


@MODES
def test_first_error_stops_queued_transfers_and_stream_delivers_it_once(
    mode: Mode,
) -> None:
    attempted: list[str] = []
    original = DockerException(["cli"], 1, b"", b"failure")

    def fail(name: str) -> FakeChild:
        attempted.append(name)
        raise original

    with observation(child_policy(1)):
        stream = MultiImageStream(NAMES, fail)
        with pytest.raises(DockerException) as raised:
            if mode == "blocking":
                map_in_contexts(fail, NAMES)
            else:
                drain(stream)
        stream.close()
        with pytest.raises(StopIteration):
            next(stream)
    assert raised.value is original
    assert attempted == [NAMES[0]]


def test_sibling_taking_a_slot_after_sizing_is_refused_immediately() -> None:
    launches = Launches(["first", "second"], parties=1)
    launches.until_refused = Event()
    admitted = Event()

    def launch(name: str) -> MappedProcessStream[tuple[str, bytes]]:
        assert admitted.wait(BOUNDED_WAIT)
        return FakeChild(launches, name)

    with observation(child_policy(2)) as state:
        stream = MultiImageStream(["first", "second"], launch)
        stream._start()
    held = state.reserve_child()
    admitted.set()
    with pytest.raises(ObservationLimitError) as refused:
        drain(stream)
    held.release()
    assert refused.value.reason == "concurrent_children"
    assert launches.peak == 1
    assert [account.consumed for account in state.child_accounts] == [0]


@pytest.mark.parametrize("operation", ["pull", "push"])
@MODES
def test_public_transfers_fit_the_observation_child_limit(
    operation: Literal["pull", "push"], mode: Mode
) -> None:
    launches = Launches(NAMES, parties=2)

    def blocking(_cli: ImageCLI, name: str, *_args: object, **_kwargs: object) -> None:
        transfer(launches)(name)

    def streaming(
        _cli: ImageCLI, name: str, *_args: object, **_kwargs: object
    ) -> FakeChild:
        return FakeChild(launches, name)

    image = DockerClient().image
    single = blocking if mode == "blocking" else streaming
    with (
        patch.object(ImageCLI, f"_{operation}_single_tag", new=single),
        patch.object(ImageCLI, "inspect", return_value=[]),
        patch("pywhales.components.image.cli_wrapper.Image"),
        observation(child_policy(2)),
    ):
        result = (image.pull if operation == "pull" else image.push)(
            NAMES, quiet=mode == "blocking", stream_logs=mode == "stream"
        )
        if mode == "stream":
            assert isinstance(result, MultiImageStream)
            assert drain(result) == NAMES
    assert launches.peak == 2
