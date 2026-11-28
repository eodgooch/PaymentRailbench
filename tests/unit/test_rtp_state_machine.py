"""The RTP Payment state machine (SPEC §4.1, §4.2): `created → settled`, or `declined`.
Instant and final: nothing follows either ending, and there's no submission or Return."""

import pytest

from railbench.payments.state_machine import Advance, Impossible, PaymentState, Stale, decide

C, SUB, SET, AV = (
    PaymentState.CREATED,
    PaymentState.SUBMITTED,
    PaymentState.SETTLED,
    PaymentState.AVAILABLE,
)
CAN, DEC = PaymentState.CANCELED, PaymentState.DECLINED


@pytest.mark.parametrize("ending", [SET, DEC])
def test_an_rtp_payment_goes_straight_from_created_to_its_ending(ending: PaymentState) -> None:
    assert decide(C, ending, rail="rtp") == Advance(path=(ending,))


@pytest.mark.parametrize(
    ("current", "reported"), [(SET, SET), (DEC, DEC), (SET, C), (DEC, C), (C, C)]
)
def test_an_rtp_event_for_a_state_already_reached_is_stale(
    current: PaymentState, reported: PaymentState
) -> None:
    assert decide(current, reported, rail="rtp") == Stale()


@pytest.mark.parametrize(
    ("current", "reported"),
    [
        (SET, DEC),  # final: a settled RTP Payment can't be declined after all
        (DEC, SET),
        (C, SUB),  # RTP has no submission step
        (C, CAN),  # nor a cancellation
        (SET, CAN),
        (C, AV),  # nor an availability step: it's a push, money leaving Mintwharf
        (SET, AV),
    ],
)
def test_anything_else_on_rtp_is_impossible(current: PaymentState, reported: PaymentState) -> None:
    decision = decide(current, reported, rail="rtp")

    assert isinstance(decision, Impossible)
    assert "RTP" in decision.reason
    assert reported.value in decision.reason


def test_ach_is_still_the_default() -> None:
    assert decide(C, SET) == Advance(path=(SUB, SET))
