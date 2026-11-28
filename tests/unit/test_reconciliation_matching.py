"""The matching rules (SPEC §7.1, ADR 0015): one Banking Day's Ledger view and report lines
in, typed Breaks out. Fridays and Tuesdays around Labor Day 2026 (Monday 7 September)
check that "the next report" means the next Banking Day's."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from railbench.reconciliation.matching import (
    AdjustmentLine,
    BreakType,
    DayView,
    Evidence,
    LedgerPayment,
    Reissue,
    RestatedLine,
    StatedLine,
    break_id,
    match,
)

THURSDAY, FRIDAY, TUESDAY, WEDNESDAY = (date(2026, 9, d) for d in (3, 4, 8, 9))
FRIDAY_0830 = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 New York
FRIDAY_1700 = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)  # after the 16:00 ACH Cutoff
REPORT_IDS = {THURSDAY: 10, FRIDAY: 11, TUESDAY: 12, WEDNESDAY: 13}


def payment(
    name: str = "a",
    *,
    amount: int = 5_000,
    direction: str = "pull",
    rail: str = "ach",
    state: str = "settled",
    settled_at: datetime | None = FRIDAY_0830,
) -> LedgerPayment:
    number = ord(name[0])
    return LedgerPayment(
        id=number,
        provider_payment_id=f"simpay_{name}",
        idempotency_key=f"key-{name}",
        rail=rail,
        direction=direction,
        currency="USD",
        amount=amount,
        state=state,
        settled_at=None if state not in ("settled", "available", "returned") else settled_at,
        evidence=Evidence(payments=(number,), transitions=(number * 10,), inbox=(number * 100,)),
    )


_rows = iter(range(1_000, 10_000))


def line(
    of: LedgerPayment,
    day: date = FRIDAY,
    *,
    amount: int | None = None,
    line_id: str | None = None,
    position: int = 1,
    type_: str = "settlement",
) -> StatedLine:
    return StatedLine(
        id=next(_rows),
        report_id=REPORT_IDS[day],
        report_date=day,
        position=position,
        line_id=line_id or f"simline_{of.provider_payment_id}_{type_}",
        type=type_,
        provider_payment_id=of.provider_payment_id,
        idempotency_key=of.idempotency_key,
        rail=of.rail,
        direction=of.direction,
        currency=of.currency,
        amount=of.signed_amount if amount is None else amount,
        settled_at=of.settled_at or FRIDAY_0830,
    )


def view(
    payments: list[LedgerPayment],
    lines: list[StatedLine],
    day: date = FRIDAY,
    adjustments: list[AdjustmentLine] | None = None,
    reissues: list[Reissue] | None = None,
) -> DayView:
    return DayView(
        provider="simulator",
        day=day,
        report_id=REPORT_IDS[day],
        payments={p.provider_payment_id: p for p in payments},
        settlement_lines=tuple(lines),
        adjustment_lines=tuple(adjustments or ()),
        reissues=tuple(reissues or ()),
    )


def kinds(found: DayView) -> list[tuple[BreakType, str, int]]:
    return [(b.break_type, b.match_key, b.exposure) for b in match(found)]


def test_a_settlement_listed_on_its_day_for_what_settled_is_no_break() -> None:
    pull, push = payment("a"), payment("p", direction="push", amount=27_000)

    assert match(view([pull, push], [line(pull), line(push, position=2)])) == []
    assert line(push).amount == -27_000  # a push is money out of Mintwharf's account


def test_a_settled_payment_on_no_report_up_to_the_next_banking_days_is_missing_at_provider() -> (
    None
):
    pull = payment()

    [found] = match(view([pull], []))

    assert (found.break_type, found.match_key, found.exposure) == (
        BreakType.MISSING_AT_PROVIDER,
        "simpay_a",
        5_000,
    )
    assert found.evidence == pull.evidence
    assert found.facts["expected_report_day"] == "2026-09-04"


def test_a_line_on_the_next_banking_days_report_is_a_timing_mismatch_raised_on_its_day() -> None:
    pull = payment()
    late = line(pull, TUESDAY)  # Monday is Labor Day: Tuesday's is the next report

    assert kinds(view([pull], [late])) == [(BreakType.TIMING_MISMATCH, "simpay_a", 5_000)]
    # Tuesday's run leaves it to Friday's, so the one disagreement is one Break.
    assert match(view([pull], [late], TUESDAY)) == []


def test_a_line_later_than_the_next_report_is_missing_on_its_day_then_late_on_its_own() -> None:
    pull = payment()
    very_late = line(pull, WEDNESDAY)

    assert kinds(view([pull], [], FRIDAY)) == [(BreakType.MISSING_AT_PROVIDER, "simpay_a", 5_000)]
    [found] = match(view([pull], [very_late], WEDNESDAY))
    assert (found.break_type, found.match_key) == (BreakType.TIMING_MISMATCH, very_late.line_id)


def test_after_the_cutoff_the_ledger_expects_the_next_banking_days_report() -> None:
    late_in_the_day = payment(settled_at=FRIDAY_1700)

    assert kinds(view([late_in_the_day], [], FRIDAY)) == []  # not expected on Friday's
    assert match(view([late_in_the_day], [line(late_in_the_day, TUESDAY)], TUESDAY)) == []
    early = line(late_in_the_day, FRIDAY)
    assert kinds(view([late_in_the_day], [early], TUESDAY)) == [
        (BreakType.TIMING_MISMATCH, "simpay_a", 5_000)
    ]
    assert match(view([late_in_the_day], [early], FRIDAY)) == []  # Tuesday's run raises it


def test_a_wrong_amount_is_an_amount_mismatch_exposing_the_difference() -> None:
    pull, push = payment("a"), payment("p", direction="push", amount=27_000)

    found = view([pull, push], [line(pull, amount=4_500), line(push, amount=-27_100, position=2)])

    assert kinds(found) == [
        (BreakType.AMOUNT_MISMATCH, "simpay_a", 500),
        (BreakType.AMOUNT_MISMATCH, "simpay_p", 100),
    ]


def test_a_line_both_late_and_wrong_is_two_breaks() -> None:
    pull = payment()

    assert kinds(view([pull], [line(pull, TUESDAY, amount=5_001)])) == [
        (BreakType.AMOUNT_MISMATCH, "simpay_a", 1),
        (BreakType.TIMING_MISMATCH, "simpay_a", 5_000),
    ]


def test_a_second_line_for_one_payment_is_a_duplicate_on_the_report_its_on() -> None:
    pull = payment()
    first, again = line(pull), line(pull, line_id="simline_again", position=2)
    later = line(pull, TUESDAY, line_id="simline_later")

    [found] = match(view([pull], [first, again]))
    assert (found.break_type, found.match_key, found.exposure) == (
        BreakType.DUPLICATE,
        "simline_again",
        5_000,
    )
    assert set(found.evidence.report_lines) == {first.id, again.id}
    assert match(view([pull], [first, later])) == []  # Friday's line is the one
    assert kinds(view([pull], [first, later], TUESDAY)) == [
        (BreakType.DUPLICATE, "simline_later", 5_000)
    ]


def test_a_line_for_no_payment_in_the_ledger_is_missing_in_ledger() -> None:
    stranger = payment("s")

    [found] = match(view([], [line(stranger, amount=-700)]))

    assert (found.break_type, found.exposure) == (BreakType.MISSING_IN_LEDGER, 700)
    assert found.evidence.payments == ()


@pytest.mark.parametrize(
    "contradiction",
    [
        {"idempotency_key": "someone-else"},
        {"rail": "rtp"},
        {"direction": "push"},
        {"currency": "X"},
    ],
)
def test_a_line_contradicting_its_payment_is_missing_in_ledger_and_the_payment_missing(
    contradiction: dict[str, str],
) -> None:
    pull = payment()
    wrong = replace(line(pull), **contradiction)  # type: ignore[arg-type]

    assert kinds(view([pull], [wrong])) == [
        (BreakType.MISSING_AT_PROVIDER, "simpay_a", 5_000),
        (BreakType.MISSING_IN_LEDGER, wrong.line_id, 5_000),
    ]


@pytest.mark.parametrize("state", ["created", "submitted", "declined", "canceled"])
def test_a_line_for_a_payment_the_ledger_has_not_settled_is_a_status_mismatch(state: str) -> None:
    unsettled = payment(state=state)

    [found] = match(view([unsettled], [line(unsettled)]))

    assert (found.break_type, found.match_key, found.exposure) == (
        BreakType.STATUS_MISMATCH,
        "simpay_a",
        5_000,
    )
    assert found.facts["ledger_state"] == state


def test_a_returned_payment_still_expects_its_settlement_line() -> None:
    returned = payment(state="returned")

    assert match(view([returned], [line(returned)])) == []
    assert kinds(view([returned], [])) == [(BreakType.MISSING_AT_PROVIDER, "simpay_a", 5_000)]


def adjustment(of: LedgerPayment, outcome: str | None, amount: int = -150) -> AdjustmentLine:
    return AdjustmentLine(
        line=line(of, TUESDAY, amount=amount, type_="adjustment", position=5),
        outcome=outcome,
        decisions=(77,),
        payment_id=of.id if outcome != "unmatched" else None,
        detail="no Payment simpay_a at simulator in the Ledger" if outcome == "unmatched" else "",
    )


def test_provider_adjustments_booked_agree_unmatched_are_missing_pending_are_status() -> None:
    pull = payment(settled_at=FRIDAY_1700)  # expected on Tuesday's report, and listed there
    listed = line(pull, TUESDAY)
    booked, unmatched, pending = (adjustment(pull, o) for o in ("booked", "unmatched", "pending"))

    assert match(view([pull], [listed], TUESDAY, [booked])) == []
    [orphan] = match(view([pull], [listed], TUESDAY, [unmatched]))
    assert (orphan.break_type, orphan.match_key, orphan.exposure) == (
        BreakType.MISSING_IN_LEDGER,
        unmatched.line.line_id,
        150,
    )
    assert orphan.evidence.provider_adjustments == (77,)
    assert orphan.facts["adjustment_outcome"] == "unmatched"
    [waiting] = match(view([pull], [listed], TUESDAY, [pending]))
    assert (waiting.break_type, waiting.exposure) == (BreakType.STATUS_MISMATCH, 150)
    assert pull.id in waiting.evidence.payments
    undecided = replace(pending, outcome=None)
    assert [b.break_type for b in match(view([pull], [listed], TUESDAY, [undecided]))] == [
        BreakType.STATUS_MISMATCH
    ]


def test_a_reissued_report_stating_other_lines_is_a_duplicate_exposing_the_difference() -> None:
    pull, other = payment("a"), payment("b", amount=900)
    listed = [line(pull), line(other, position=2)]

    def reissue(*lines: tuple[str, int]) -> Reissue:
        return Reissue(
            report_id=99,
            body_sha256="ab" * 32,
            lines=tuple(RestatedLine(line_id, "USD", amount) for line_id, amount in lines),
        )

    same = reissue((listed[0].line_id, 5_000), (listed[1].line_id, 900))
    assert match(view([pull, other], listed, reissues=[same])) == []

    changed = reissue((listed[0].line_id, 4_000), ("simline_new", 300))
    [found] = match(view([pull, other], listed, reissues=[changed]))
    # 1,000 less on one line, 900 dropped, 300 added.
    assert (found.break_type, found.match_key, found.exposure) == (
        BreakType.DUPLICATE,
        f"report:{'ab' * 32}:USD",
        2_200,
    )
    assert found.evidence.reports == (REPORT_IDS[FRIDAY], 99)
    assert set(found.evidence.report_lines) == {listed[0].id, listed[1].id}


def test_matching_is_deterministic_whatever_order_its_inputs_come_in() -> None:
    payments = [payment(n, amount=100 * (i + 1)) for i, n in enumerate("abcdef")]
    lines = [line(p, amount=p.amount + 1, position=i) for i, p in enumerate(payments[:4], 1)]

    forwards = match(view(payments, lines))
    backwards = match(view(list(reversed(payments)), list(reversed(lines))))

    assert forwards == backwards
    assert [b.facts for b in forwards] == [b.facts for b in backwards]
    assert [b.match_key for b in forwards] == sorted(b.match_key for b in forwards)


def test_a_breaks_id_is_the_same_for_the_same_disagreement_and_differs_otherwise() -> None:
    one = break_id("simulator", FRIDAY, BreakType.DUPLICATE, "simline_x")

    assert one == break_id("simulator", FRIDAY, BreakType.DUPLICATE, "simline_x")
    assert one.startswith("brk_") and len(one) == 28
    assert (
        len(
            {
                one,
                break_id("simulator", TUESDAY, BreakType.DUPLICATE, "simline_x"),
                break_id("simulator", FRIDAY, BreakType.TIMING_MISMATCH, "simline_x"),
                break_id("simulator", FRIDAY, BreakType.DUPLICATE, "simline_y"),
            }
        )
        == 4
    )
