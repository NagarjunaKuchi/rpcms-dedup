import threading
from datetime import datetime, timezone

import pytest

from rpcms_dedup import service
from rpcms_dedup.db import make_session_factory
from rpcms_dedup.models import AssistanceEvent, EventStatus, ReferralStatus
from tests.conftest import HMAC_KEY

NOW = datetime(2026, 3, 5, 11, 42, tzinfo=timezone.utc)


def req(**kw):
    base = dict(
        idempotency_key="key-00000001",
        progres_id="UGA-00423318",
        assistance_type="cash-for-food",
        cycle="2026-03",
        partner_id="NHU",
        record_id="DST-NHU-0305-00214",
        recorded_at=NOW,
        amount=150000,
        currency="UGX",
    )
    base.update(kw)
    return service.RecordRequest(**base)


def test_token_is_stable_keyed_and_normalised():
    a = service.make_token(HMAC_KEY, "UGA-00423318")
    assert a == service.make_token(HMAC_KEY, "  uga-00423318 ")
    assert a != service.make_token("x" * 40, "UGA-00423318")
    assert "00423318" not in a and len(a) == 64


def test_first_record_wins_and_second_partner_is_refused(session):
    first = service.record_assistance(session, HMAC_KEY, req())
    assert first.outcome == service.Outcome.RECORDED
    second = service.record_assistance(
        session,
        HMAC_KEY,
        req(idempotency_key="key-00000002", partner_id="WNRP", record_id="DST-WNRP-1"),
    )
    assert second.outcome == service.Outcome.DUPLICATE
    assert second.primary.id == first.event.id
    assert second.event.status == EventStatus.REJECTED_DUPLICATE
    assert second.referral.status == ReferralStatus.OPEN


def test_same_person_other_cycle_or_type_is_not_duplicate(session):
    service.record_assistance(session, HMAC_KEY, req())
    other_cycle = service.record_assistance(session, HMAC_KEY, req(idempotency_key="key-00000002", cycle="2026-04"))
    other_type = service.record_assistance(
        session, HMAC_KEY, req(idempotency_key="key-00000003", assistance_type="shelter-kit")
    )
    assert other_cycle.outcome == other_type.outcome == service.Outcome.RECORDED


def test_normalised_inputs_collide(session):
    service.record_assistance(session, HMAC_KEY, req())
    dup = service.record_assistance(
        session,
        HMAC_KEY,
        req(idempotency_key="key-00000002", progres_id=" uga-00423318", assistance_type="CASH-FOR-FOOD", cycle="2026-03"),
    )
    assert dup.outcome == service.Outcome.DUPLICATE


def test_retry_with_same_key_and_payload_is_a_replay(session):
    first = service.record_assistance(session, HMAC_KEY, req())
    again = service.record_assistance(session, HMAC_KEY, req())
    assert again.outcome == service.Outcome.REPLAY
    assert again.event.id == first.event.id


def test_key_reuse_with_different_payload_is_rejected(session):
    service.record_assistance(session, HMAC_KEY, req())
    with pytest.raises(service.IdempotencyKeyReused):
        service.record_assistance(session, HMAC_KEY, req(amount=999))


def test_offline_loser_is_kept_as_conflict_not_lost(session):
    first = service.record_assistance(session, HMAC_KEY, req())
    off = service.record_assistance(
        session,
        HMAC_KEY,
        req(idempotency_key="key-00000002", partner_id="WNRP", record_id="DST-WNRP-9", offline=True),
    )
    assert off.outcome == service.Outcome.CONFLICT
    assert off.event.status == EventStatus.CONFLICT
    assert off.primary.id == first.event.id


def test_check_does_not_reserve_and_reuses_open_referral(session):
    miss = service.check_duplicate(
        session, HMAC_KEY, progres_id="UGA-1", assistance_type="cash", cycle="c1", partner_id="NHU"
    )
    assert not miss.duplicate and miss.existing is None
    service.record_assistance(session, HMAC_KEY, req())
    kw = dict(progres_id="UGA-00423318", assistance_type="cash-for-food", cycle="2026-03", partner_id="WNRP")
    one = service.check_duplicate(session, HMAC_KEY, **kw)
    two = service.check_duplicate(session, HMAC_KEY, **kw)
    assert one.duplicate and one.referral.id == two.referral.id
    quiet = service.check_duplicate(session, HMAC_KEY, open_referral=False, **{**kw, "partner_id": "OTHER"})
    assert quiet.duplicate and quiet.referral is None


def test_resolve_referral(session):
    service.record_assistance(session, HMAC_KEY, req())
    dup = service.record_assistance(session, HMAC_KEY, req(idempotency_key="key-00000002", partner_id="WNRP"))
    r = service.resolve_referral(
        session, dup.referral.id, resolution="CONFIRMED_DUPLICATE", resolved_by="commandant-1", note=None
    )
    assert r.status == ReferralStatus.RESOLVED and r.resolved_at is not None
    with pytest.raises(service.ReferralAlreadyResolved):
        service.resolve_referral(session, r.id, resolution="FALSE_POSITIVE", resolved_by="x", note=None)
    with pytest.raises(service.ReferralNotFound):
        service.resolve_referral(session, "nope", resolution="FALSE_POSITIVE", resolved_by="x", note=None)


def test_raw_progres_id_is_never_stored(session, engine):
    service.record_assistance(session, HMAC_KEY, req())
    from sqlalchemy import text

    with engine.connect() as c:
        dump = " ".join(str(v) for row in c.execute(text("select * from assistance_event")) for v in row)
    assert "00423318" not in dump


def test_concurrent_commits_yield_exactly_one_winner(engine):
    factory = make_session_factory(engine)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int):
        with factory() as s:
            barrier.wait()
            r = service.record_assistance(
                s, HMAC_KEY, req(idempotency_key=f"key-race-{i:04d}", partner_id=f"P{i}", record_id=f"R{i}")
            )
            with lock:
                outcomes.append(r.outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(service.Outcome.RECORDED) == 1
    assert outcomes.count(service.Outcome.DUPLICATE) == 7
    with factory() as s:
        winners = s.query(AssistanceEvent).filter(AssistanceEvent.dedup_slot.is_not(None)).count()
    assert winners == 1


def test_concurrent_retry_of_same_key_is_one_record(engine):
    factory = make_session_factory(engine)
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def worker():
        with factory() as s:
            barrier.wait()
            outcomes.append(service.record_assistance(s, HMAC_KEY, req()).outcome)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(service.Outcome.RECORDED) == 1
    assert outcomes.count(service.Outcome.REPLAY) == 5
