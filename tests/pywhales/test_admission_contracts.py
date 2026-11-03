"""Validation, safe reporting, and isolated admission contracts."""

from contextlib import nullcontext
from dataclasses import FrozenInstanceError

import pytest

from pywhales.admission import (
    AdmissionOwner,
    AdmissionRejected,
    ClassLimit,
    DemandSnapshot,
    EndpointIdentity,
    OutcomeCounts,
    PressureClass,
    RejectionReason,
    SaturationPolicy,
)
from pywhales.admission._state import UINT64_MAX, Bucket, Outcome


@pytest.mark.parametrize("token", ["", "_bad", "a/b", "a:b", "a b", "é", "a" * 65])
def test_identity_rejects_unsafe_tokens(token: str) -> None:
    with pytest.raises(ValueError) as raised:
        EndpointIdentity(token)
    assert token not in str(raised.value) or not token
    with pytest.raises(ValueError):
        PressureClass(token)


@pytest.mark.parametrize(
    "active,pending", [(0, 1), (-1, 1), (True, 1), (1, -1), (1, True)]
)
def test_limit_rejects_invalid_counts(active: int, pending: int) -> None:
    with pytest.raises(ValueError):
        ClassLimit(active, pending, SaturationPolicy.WAIT)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_timeout_validation_precedes_request_registration(timeout: float) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    with pytest.raises(ValueError):
        owner.request(pressure, queue_timeout=timeout)
    assert owner.snapshot().classes[0].pending == 0


def test_owner_copies_limits_and_snapshots_are_frozen() -> None:
    pressure = PressureClass("read")
    limits = {pressure: ClassLimit(1, 0)}
    owner = AdmissionOwner(EndpointIdentity("local"), limits)
    limits.clear()
    assert owner.request(pressure).call(lambda: 7) == 7
    snapshot = owner.snapshot()
    assert snapshot.classes[0].outcomes.completed == 1
    attribute = "active"
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot.classes[0], attribute, 100)
    with pytest.raises(ValueError):
        AdmissionOwner(EndpointIdentity("empty"), {})
    with pytest.raises(ValueError):
        owner.request(PressureClass("unknown"))


def test_equal_endpoint_aliases_do_not_merge_owners() -> None:
    pressure = PressureClass("transfer")
    first = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    second = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    assert (
        first.request(pressure).call(lambda: second.request(pressure).call(lambda: 3))
        == 3
    )
    assert first.snapshot().classes[0].outcomes.completed == 1
    assert second.snapshot().classes[0].outcomes.completed == 1


def test_rejection_has_bounded_reason_and_safe_text() -> None:
    error = AdmissionRejected(RejectionReason.QUEUE_FULL)
    assert error.reason is RejectionReason.QUEUE_FULL
    assert str(error) == "admission rejected: queue_full"


def test_close_is_idempotent_and_snapshots_remain_immutable() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    dormant = [owner.request(pressure) for _ in range(100)]
    before = owner.snapshot()

    owner.close()
    owner.close()

    after = owner.snapshot()
    assert not before.closed and after.closed
    assert after.classes[0].pending == 0
    assert after.classes[0].outcomes.rejected_closed == 0
    assert len(dormant) == 100
    for target, attribute in (
        (after, "closed"),
        (after.classes[0].outcomes, "rejected_closed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, attribute, 1)


def test_closed_rejections_saturate_separately_without_wrapping() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    owner._buckets[pressure].counts[Outcome.REJECTED_CLOSED] = UINT64_MAX - 1
    owner.close()

    for _ in range(2):
        with pytest.raises(AdmissionRejected) as raised:
            owner.request(pressure).call(lambda: pytest.fail("closed callback ran"))
        assert raised.value.reason is RejectionReason.CLOSED

    counts = owner.snapshot().classes[0].outcomes
    assert counts.rejected_closed == UINT64_MAX and counts.saturated
    assert (
        counts.failed,
        counts.rejected_busy,
        counts.rejected_full,
        counts.cancelled,
        counts.queue_timed_out,
    ) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, "2"])
def test_limit_rejects_invalid_demand_capacity(capacity: int) -> None:
    # Given / When / Then: invalid capacity fails at construction.
    with pytest.raises(ValueError):
        ClassLimit(1, 0, demand_capacity=capacity)


@pytest.mark.parametrize("demand", [0, -1, True, 1.5, "2", None, lambda: 1])
def test_invalid_demand_never_registers_a_request(demand: int) -> None:
    # Given
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(2, 2)})
    # When / Then
    with pytest.raises(ValueError):
        owner.request(pressure, demand=demand)
    snapshot = owner.snapshot().classes[0]
    assert snapshot.demand == DemandSnapshot(0, 0, 0, 0)
    assert (snapshot.active, snapshot.pending) == (0, 0)


def test_oversized_demand_fails_before_work_is_supplied() -> None:
    # Given
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"), {pressure: ClassLimit(2, 2, demand_capacity=3)}
    )
    # When / Then
    with pytest.raises(ValueError):
        owner.request(pressure, demand=4)
    assert owner.snapshot().classes[0].demand == DemandSnapshot(0, 0, 0, 0)


def test_demand_snapshots_are_immutable_and_independent_of_request_counts() -> None:
    # Given
    pressure = PressureClass("read")
    limit = ClassLimit(3, 2, SaturationPolicy.REJECT, demand_capacity=10)
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: limit})
    with owner.request(pressure, demand=4).stream(
        lambda: nullcontext(iter(()))
    ) as stream:
        before = owner.snapshot().classes[0]
        # When
        assert stream.try_resize(7)
        # Then
        after = owner.snapshot().classes[0]
        assert (before.active, after.active, after.active_high_water) == (1, 1, 1)
        assert before.demand == DemandSnapshot(4, 0, 4, 0)
        assert after.demand == DemandSnapshot(7, 0, 7, 0)
        attribute = "active"
        with pytest.raises(FrozenInstanceError):
            setattr(after.demand, attribute, 0)
    assert owner.snapshot().classes[0].demand == DemandSnapshot(0, 0, 7, 0)


@pytest.mark.parametrize("capacity", [-1, True, False, 1.5, "2"])
def test_limit_rejects_invalid_pending_demand_capacity(capacity: int) -> None:
    with pytest.raises(ValueError):
        ClassLimit(1, 1, pending_demand_capacity=capacity)


@pytest.mark.parametrize("capacity", [-1, True, False, 1.5, "2"])
def test_owner_rejects_invalid_pending_demand_capacity(capacity: int) -> None:
    pressure = PressureClass("read")
    with pytest.raises(ValueError):
        AdmissionOwner(
            EndpointIdentity("local"),
            {pressure: ClassLimit(1, 1)},
            owner_pending_demand_capacity=capacity,
        )


def test_outcome_snapshots_preserve_legacy_positional_meanings() -> None:
    legacy = OutcomeCounts(1, 2, 3, 4, 5, 6, 7, 8, True)
    assert legacy.rejected_closed == 8
    assert legacy.saturated is True
    assert legacy.rejected_pending_demand_full == 0
    assert legacy.rejected_owner_pending_demand_full == 0
    bucket = Bucket(ClassLimit(1, 1))
    bucket.counts[:] = range(1, 11)
    assert bucket.snapshot(PressureClass("read")).outcomes == OutcomeCounts(
        1, 2, 3, 4, 5, 6, 7, 8, False, 9, 10
    )


def test_pending_demand_rejections_saturate_without_counting_twice() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1, pending_demand_capacity=0)},
    )
    owner._buckets[pressure].counts[Outcome.REJECTED_PENDING_DEMAND_FULL] = (
        UINT64_MAX - 1
    )
    with owner.request(pressure).stream(lambda: nullcontext(iter(()))):
        for _ in range(2):
            request = owner.request(pressure)
            with pytest.raises(AdmissionRejected) as raised:
                request.call(lambda: pytest.fail("rejected work ran"))
            assert raised.value.reason is RejectionReason.PENDING_DEMAND_FULL
            assert not request.cancel()
            with pytest.raises(RuntimeError):
                request.call(lambda: pytest.fail("reused work ran"))
    snapshot = owner.snapshot().classes[0]
    assert snapshot.outcomes == OutcomeCounts(0, 0, 1, 0, 0, 0, 0, 0, True, UINT64_MAX)
    assert snapshot.demand == DemandSnapshot(0, 0, 1, 0)
