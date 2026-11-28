"""The ACH Payment state machine (SPEC §4.1, §4.2): forward only; an event for a state
already passed is stale and does nothing; an impossible one is an exception, never a crash."""

import pytest

from railbench.payments.state_machine import Advance, Impossible, PaymentState, Stale, decide

C, SUB, SET, AV = (
    PaymentState.CREATED,
    PaymentState.SUBMITTED,
    PaymentState.SETTLED,
    PaymentState.AVAILABLE,
)
CAN, DEC = PaymentState.CANCELED, PaymentState.DECLINED


@pytest.mark.parametrize(
    ("current", "reported", "path"),
    [
        (C, SUB, [SUB]),
        (SUB, SET, [SET]),
        (SET, AV, [AV]),
        # A Provider Event can skip a state it never reported; the path fills it in.
        (C, SET, [SUB, SET]),
        (C, CAN, [CAN]),
        (C, DEC, [DEC]),
        (SUB, CAN, [CAN]),
        (SUB, DEC, [DEC]),
    ],
)
def test_the_payment_moves_forward(
    current: PaymentState, reported: PaymentState, path: list[PaymentState]
) -> None:
    assert decide(current, reported, rail="ach", direction="pull") == Advance(path=tuple(path))


@pytest.mark.parametrize(
    ("current", "reported"),
    [
        (SUB, SUB),
        (SUB, C),
        (SET, SUB),
        (SET, SET),
        (AV, SET),
        (AV, SUB),
        (DEC, DEC),
        (CAN, CAN),
        (DEC, C),
    ],
)
def test_an_event_for_a_state_already_passed_is_stale(
    current: PaymentState, reported: PaymentState
) -> None:
    assert decide(current, reported, rail="ach", direction="pull") == Stale()


@pytest.mark.parametrize("ending", [DEC, CAN])
def test_a_submitted_for_a_payment_submitted_before_it_ended_is_stale(ending: PaymentState) -> None:
    assert decide(ending, SUB, reached=[C, SUB, ending], rail="ach", direction="pull") == Stale()


@pytest.mark.parametrize("ending", [DEC, CAN])
def test_a_submitted_for_a_payment_that_ended_without_being_submitted_is_impossible(
    ending: PaymentState,
) -> None:
    """It went created → canceled|declined, so a `submitted` now may mean the Provider
    really sent the debit after all: an exception, not something to shrug off."""
    assert isinstance(
        decide(ending, SUB, reached=[C, ending], rail="ach", direction="pull"), Impossible
    )


@pytest.mark.parametrize(
    ("current", "reported"),
    [
        (SET, DEC),  # declined after the money settled
        (AV, CAN),
        (DEC, SET),  # settled after a decline
        (CAN, SUB),
        (DEC, SUB),
        (DEC, CAN),
        (C, AV),  # only the Ledger decides Available, and only after settlement
        (SUB, AV),
    ],
)
def test_an_impossible_transition_is_an_exception(
    current: PaymentState, reported: PaymentState
) -> None:
    decision = decide(current, reported, rail="ach", direction="pull")

    assert isinstance(decision, Impossible)
    assert current.value in decision.reason
    assert reported.value in decision.reason


RET = PaymentState.RETURNED


@pytest.mark.parametrize("current", [SET, AV])
def test_a_settled_or_available_payment_can_be_returned(current: PaymentState) -> None:
    assert decide(current, RET, rail="ach", direction="pull") == Advance(path=(RET,))


@pytest.mark.parametrize("current", [C, SUB])
def test_a_return_for_a_payment_never_settled_is_impossible(current: PaymentState) -> None:
    """SPEC §4.2's own example: the Ledger never heard it settled, so a Return now is an
    exception record, never a Ledger write, even if the settlement is only late."""
    decision = decide(current, RET, rail="ach", direction="pull")

    assert isinstance(decision, Impossible)
    assert "never settled" in decision.reason


@pytest.mark.parametrize("ending", [DEC, CAN])
def test_a_return_after_a_decline_or_cancellation_is_impossible(ending: PaymentState) -> None:
    assert isinstance(
        decide(ending, RET, reached=[C, SUB, ending], rail="ach", direction="pull"), Impossible
    )


@pytest.mark.parametrize("reported", [C, SUB, SET, AV, RET])
def test_a_returned_payment_has_passed_everything_on_its_way(reported: PaymentState) -> None:
    """Returned is final. A late settlement, a second Return, or the availability step after
    a Return report nothing new."""
    assert (
        decide(RET, reported, reached=[C, SUB, SET, RET], rail="ach", direction="pull") == Stale()
    )


@pytest.mark.parametrize("reported", [DEC, CAN])
def test_a_returned_payment_cannot_be_declined_or_canceled(reported: PaymentState) -> None:
    assert isinstance(
        decide(RET, reported, reached=[C, SUB, SET, RET], rail="ach", direction="pull"), Impossible
    )


@pytest.mark.parametrize("current", [C, SET])
def test_rtp_has_no_returns(current: PaymentState) -> None:
    decision = decide(current, RET, rail="rtp", direction="push")

    assert isinstance(decision, Impossible)
    assert "RTP" in decision.reason


@pytest.mark.parametrize("current", [SET, AV])
def test_a_push_is_never_returned(current: PaymentState) -> None:
    decision = decide(current, RET, rail="ach", direction="push")

    assert isinstance(decision, Impossible)
    assert "push" in decision.reason
