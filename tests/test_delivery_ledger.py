"""The delivery ledger: the pending-row pattern, what a stale row means, what is remembered across
runs, and the lock that keeps two deliveries of a run apart."""

from datetime import date, timedelta

import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import IntegrityError

from feasibility.delivery import ledger
from feasibility.delivery.errors import DeliveryBusyError
from feasibility.tables import sourcing_run

SHA = "c" * 64
OTHER_SHA = "d" * 64


def make_run(engine: Engine, day: int = 1) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(
                market="dallas",
                as_of=date(2026, 10, day),
                status="completed",
                sync_status="skipped",
            )
            .returning(sourcing_run.c.id)
        ).scalar_one()


def age(engine: Engine, minutes: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE delivery SET updated_at = now() - :age"),
            {"age": timedelta(minutes=minutes)},
        )


def test_a_call_is_recorded_before_it_is_made_and_ended_after(engine: Engine) -> None:
    run = make_run(engine)

    ledger.start(engine, run, "slack", "digest", "mock", SHA)
    during = ledger.read(engine, run, "slack", "digest", "mock")
    ledger.finish(engine, run, "slack", "digest", "mock", "sent", remote_ref="1700000000.000100")
    after = ledger.read(engine, run, "slack", "digest", "mock")

    assert during is not None and (during.status, during.attempts) == ("sending", 1)
    assert after is not None and (after.status, after.remote_ref) == ("sent", "1700000000.000100")
    assert after.content_sha256 == SHA and after.error_code is None


def test_trying_again_counts_the_attempt_and_clears_the_last_error(engine: Engine) -> None:
    run = make_run(engine)
    ledger.start(engine, run, "notion", "row:7", "mock", SHA)
    ledger.finish(engine, run, "notion", "row:7", "mock", "failed", error_code="http_429")

    ledger.start(engine, run, "notion", "row:7", "mock", OTHER_SHA)

    state = ledger.read(engine, run, "notion", "row:7", "mock")
    assert state is not None
    assert (state.status, state.attempts, state.error_code) == ("sending", 2, None)
    assert state.content_sha256 == OTHER_SHA


def test_a_sent_row_without_a_reference_is_refused_and_so_is_an_odd_end(engine: Engine) -> None:
    run = make_run(engine)
    ledger.start(engine, run, "slack", "digest", "mock", SHA)

    with pytest.raises(IntegrityError):
        ledger.finish(engine, run, "slack", "digest", "mock", "sent")
    with pytest.raises(ValueError, match="sent, failed or unknown"):
        ledger.finish(engine, run, "slack", "digest", "mock", "skipped")


def test_a_row_still_sending_after_ten_minutes_reads_as_unknown(engine: Engine) -> None:
    run = make_run(engine)
    ledger.start(engine, run, "slack", "digest", "mock", SHA)

    age(engine, 9)
    young = ledger.read(engine, run, "slack", "digest", "mock")
    age(engine, 11)
    old = ledger.read(engine, run, "slack", "digest", "mock")

    assert young is not None and young.status == "sending"
    assert old is not None and old.status == "unknown"
    assert [r.status for r in ledger.rows_for_run(engine, run)] == ["unknown"]


def test_an_item_has_one_row_per_run_target_and_mode(engine: Engine) -> None:
    run = make_run(engine)
    for mode in ("mock", "live"):
        ledger.start(engine, run, "slack", "digest", mode, SHA)

    assert [(r.mode, r.status) for r in ledger.rows_for_run(engine, run)] == [
        ("live", "sending"),
        ("mock", "sending"),
    ]
    assert ledger.read(engine, run, "slack", "digest", "mock") is not None
    assert ledger.read(engine, run, "notion", "digest", "mock") is None


def test_what_is_remembered_is_the_newest_delivered_row_of_the_item_in_any_run(
    engine: Engine,
) -> None:
    first, second, third = make_run(engine, 1), make_run(engine, 2), make_run(engine, 3)
    ledger.start(engine, first, "notion", "row:7", "mock", SHA)
    ledger.finish(engine, first, "notion", "row:7", "mock", "sent", remote_ref="page-one")
    ledger.start(engine, second, "notion", "row:7", "mock", OTHER_SHA)
    ledger.finish(engine, second, "notion", "row:7", "mock", "failed", error_code="http_500")
    ledger.start(engine, third, "notion", "row:7", "mock", OTHER_SHA)  # still sending

    remembered = ledger.remembered(engine, "notion", "row:7", "mock")

    assert remembered is not None
    assert (remembered.run_id, remembered.remote_ref, remembered.content_sha256) == (
        first,
        "page-one",
        SHA,
    )
    assert ledger.remembered(engine, "notion", "row:7", "live") is None
    assert ledger.remembered(engine, "notion", "row:8", "mock") is None


def test_a_skipped_row_keeps_where_the_content_already_is_and_a_sent_row_is_left_alone(
    engine: Engine,
) -> None:
    first, second = make_run(engine, 1), make_run(engine, 2)
    ledger.start(engine, first, "notion", "row:7", "mock", SHA)
    ledger.finish(engine, first, "notion", "row:7", "mock", "sent", remote_ref="page-one")

    ledger.note_skipped(engine, second, "notion", "row:7", "mock", SHA, "page-one")
    ledger.note_skipped(engine, first, "notion", "row:7", "mock", SHA, "page-one")

    states = {
        r.status for r in ledger.rows_for_run(engine, first) + ledger.rows_for_run(engine, second)
    }
    assert states == {"sent", "skipped"}
    remembered = ledger.remembered(engine, "notion", "row:7", "mock")
    assert remembered is not None and remembered.run_id == second


def test_dropping_a_target_forgets_only_what_is_unsettled_for_that_run_target_and_mode(
    engine: Engine,
) -> None:
    run, other = make_run(engine, 1), make_run(engine, 2)
    for r in (run, other):
        for target, item in (("slack", "digest"), ("slack", "file:7"), ("notion", "row:7")):
            for mode in ("mock", "live"):
                ledger.start(engine, r, target, item, mode, SHA)
    ledger.finish(engine, run, "slack", "digest", "mock", "sent", remote_ref="1.1")
    ledger.finish(engine, run, "slack", "file:7", "mock", "unknown", error_code="network_error")

    assert ledger.drop(engine, run, "slack", "mock") == 1  # the unknown file, not the sent digest

    kept = {(r.target, r.item, r.mode) for r in ledger.rows_for_run(engine, run)}
    assert ("slack", "digest", "mock") in kept and ("slack", "file:7", "mock") not in kept
    assert len(kept) == 5
    assert len(ledger.rows_for_run(engine, other)) == 6


def test_deleting_a_run_deletes_its_ledger(engine: Engine) -> None:
    run = make_run(engine)
    ledger.start(engine, run, "slack", "digest", "mock", SHA)

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM sourcing_run WHERE id = :r"), {"r": run})

    assert ledger.rows_for_run(engine, run) == []


def test_the_lock_keeps_a_second_delivery_of_the_same_run_out_until_the_first_ends(
    engine: Engine,
) -> None:
    run, other = make_run(engine, 1), make_run(engine, 2)

    with ledger.run_lock(engine, run):
        with pytest.raises(DeliveryBusyError, match="busy"), ledger.run_lock(engine, run):
            pytest.fail("a second delivery of the same run got the lock")
        with ledger.run_lock(engine, other):  # another run is another lock
            pass

    with ledger.run_lock(engine, run):  # and the first is free again
        pass


def test_the_lock_is_freed_when_the_delivery_raises(engine: Engine) -> None:
    run = make_run(engine)

    with pytest.raises(RuntimeError, match="boom"), ledger.run_lock(engine, run):
        raise RuntimeError("boom")

    with ledger.run_lock(engine, run):
        pass


def test_no_ledger_statement_is_inside_a_transaction_the_lock_holds(engine: Engine) -> None:
    run = make_run(engine)

    with ledger.run_lock(engine, run):
        # The lock is a session lock on its own connection, so these commit at once.
        ledger.start(engine, run, "slack", "digest", "mock", SHA)
        with engine.connect() as other:
            seen = other.execute(text("SELECT count(*) FROM delivery")).scalar_one()

    assert seen == 1
