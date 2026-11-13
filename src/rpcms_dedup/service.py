from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AssistanceEvent,
    EventStatus,
    Referral,
    ReferralReason,
    ReferralStatus,
)


class IdempotencyKeyReused(Exception):
    """The same idempotency key arrived with a different payload."""


class ReferralNotFound(Exception):
    pass


class ReferralAlreadyResolved(Exception):
    pass


def make_token(hmac_key: str, progres_id: str) -> str:
    """Keyed hash of the ProGres ID: the cross-partner duplicate key.

    The ID is normalised (trimmed, upper-cased) so the same card scanned by two partners
    always maps to the same token. The raw ID is never stored.
    """
    normalised = progres_id.strip().upper()
    return hmac.new(hmac_key.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def make_slot(token: str, assistance_type: str, cycle: str) -> str:
    return f"{token}|{assistance_type.strip().upper()}|{cycle.strip().upper()}"


@dataclass
class RecordRequest:
    idempotency_key: str
    progres_id: str
    assistance_type: str
    cycle: str
    partner_id: str
    record_id: str
    recorded_at: datetime
    amount: float | None = None
    currency: str | None = None
    auth_txn_ref: str | None = None
    officer_id: str | None = None
    offline: bool = False


@dataclass
class CheckResult:
    duplicate: bool
    token: str
    existing: AssistanceEvent | None
    referral: Referral | None = None


class Outcome:
    RECORDED = "RECORDED"
    REPLAY = "REPLAY"  # same idempotency key and payload seen before; nothing changed
    DUPLICATE = "DUPLICATE"  # online commit refused
    CONFLICT = "CONFLICT"  # offline record stored but flagged


@dataclass
class RecordResult:
    outcome: str
    event: AssistanceEvent
    primary: AssistanceEvent | None = None
    referral: Referral | None = None


def _payload_hash(req: RecordRequest, token: str) -> str:
    body = {
        "token": token,
        "type": req.assistance_type.strip().upper(),
        "cycle": req.cycle.strip().upper(),
        "partner": req.partner_id,
        "record_id": req.record_id,
        "amount": None if req.amount is None else f"{req.amount:.2f}",
        "currency": req.currency,
        "offline": req.offline,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _find_primary(session: Session, slot: str) -> AssistanceEvent | None:
    return session.scalars(select(AssistanceEvent).where(AssistanceEvent.dedup_slot == slot)).first()


def _find_by_key(session: Session, key: str) -> AssistanceEvent | None:
    return session.scalars(select(AssistanceEvent).where(AssistanceEvent.idempotency_key == key)).first()


def _open_referral(
    session: Session,
    *,
    reason: str,
    token: str,
    assistance_type: str,
    cycle: str,
    partner_id: str,
    primary: AssistanceEvent,
    conflicting: AssistanceEvent | None,
) -> Referral:
    """Create an OPEN referral, or reuse an identical one already open (repeat checks)."""
    if conflicting is None:
        existing = session.scalars(
            select(Referral).where(
                Referral.status == ReferralStatus.OPEN,
                Referral.reason == reason,
                Referral.token == token,
                Referral.assistance_type == assistance_type.strip().upper(),
                Referral.cycle == cycle.strip().upper(),
                Referral.requesting_partner_id == partner_id,
                Referral.conflicting_event_id.is_(None),
            )
        ).first()
        if existing:
            return existing
    ref = Referral(
        reason=reason,
        token=token,
        assistance_type=assistance_type.strip().upper(),
        cycle=cycle.strip().upper(),
        requesting_partner_id=partner_id,
        primary_event_id=primary.id,
        conflicting_event_id=conflicting.id if conflicting else None,
    )
    session.add(ref)
    return ref


def check_duplicate(
    session: Session,
    hmac_key: str,
    *,
    progres_id: str,
    assistance_type: str,
    cycle: str,
    partner_id: str,
    open_referral: bool = True,
) -> CheckResult:
    """Read-side check before a handover. Does not reserve anything: the commit does."""
    token = make_token(hmac_key, progres_id)
    primary = _find_primary(session, make_slot(token, assistance_type, cycle))
    if primary is None:
        return CheckResult(duplicate=False, token=token, existing=None)
    referral = None
    if open_referral:
        referral = _open_referral(
            session,
            reason=ReferralReason.ONLINE_CHECK,
            token=token,
            assistance_type=assistance_type,
            cycle=cycle,
            partner_id=partner_id,
            primary=primary,
            conflicting=None,
        )
        session.commit()
    return CheckResult(duplicate=True, token=token, existing=primary, referral=referral)


def _new_event(req: RecordRequest, token: str, payload_hash: str, status: str, slot: str | None) -> AssistanceEvent:
    recorded_at = req.recorded_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    return AssistanceEvent(
        idempotency_key=req.idempotency_key,
        payload_hash=payload_hash,
        token=token,
        assistance_type=req.assistance_type.strip().upper(),
        cycle=req.cycle.strip().upper(),
        dedup_slot=slot,
        partner_id=req.partner_id,
        record_id=req.record_id,
        amount=req.amount,
        currency=req.currency,
        auth_txn_ref=req.auth_txn_ref,
        officer_id=req.officer_id,
        offline=req.offline,
        status=status,
        recorded_at=recorded_at,
    )


def _resolve_replay(existing: AssistanceEvent, payload_hash: str) -> RecordResult:
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyReused(existing.idempotency_key)
    return RecordResult(outcome=Outcome.REPLAY, event=existing)


def _store_loser(session: Session, req, token, payload_hash, primary) -> RecordResult:
    """Store an event that lost the slot, plus a referral. Offline losers are kept as
    CONFLICT (the handover physically happened); online losers are refused."""
    if req.offline:
        status, outcome, reason = EventStatus.CONFLICT, Outcome.CONFLICT, ReferralReason.OFFLINE_SYNC
    else:
        status, outcome, reason = EventStatus.REJECTED_DUPLICATE, Outcome.DUPLICATE, ReferralReason.COMMIT_REFUSED
    event = _new_event(req, token, payload_hash, status, None)
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        # The same idempotency key was committed concurrently (a retry): treat as a replay.
        if event in session:
            session.expunge(event)
        existing = _find_by_key(session, req.idempotency_key)
        if existing is None:  # pragma: no cover - some other constraint failed
            raise
        return _resolve_replay(existing, payload_hash)
    referral = _open_referral(
        session,
        reason=reason,
        token=token,
        assistance_type=req.assistance_type,
        cycle=req.cycle,
        partner_id=req.partner_id,
        primary=primary,
        conflicting=event,
    )
    session.commit()
    return RecordResult(outcome=outcome, event=event, primary=primary, referral=referral)


def record_assistance(session: Session, hmac_key: str, req: RecordRequest) -> RecordResult:
    """Atomic commit of an assistance event.

    Safe to retry (idempotency key) and safe under concurrency: the unique ``dedup_slot``
    decides the winner at the database, not in application code.
    """
    token = make_token(hmac_key, req.progres_id)
    payload_hash = _payload_hash(req, token)

    existing = _find_by_key(session, req.idempotency_key)
    if existing:
        return _resolve_replay(existing, payload_hash)

    slot = make_slot(token, req.assistance_type, req.cycle)
    primary = _find_primary(session, slot)
    if primary is not None:
        return _store_loser(session, req, token, payload_hash, primary)

    event = _new_event(req, token, payload_hash, EventStatus.RECORDED, slot)
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        # Lost a race: either the same key was committed concurrently (a retry) or another
        # partner took the slot first. Re-read to find out which.
        if event in session:
            session.expunge(event)
        existing = _find_by_key(session, req.idempotency_key)
        if existing:
            return _resolve_replay(existing, payload_hash)
        primary = _find_primary(session, slot)
        if primary is None:  # pragma: no cover - would mean the winner rolled back
            raise
        return _store_loser(session, req, token, payload_hash, primary)
    session.commit()
    return RecordResult(outcome=Outcome.RECORDED, event=event)


def list_referrals(session: Session, status: str | None = None, limit: int = 100) -> list[Referral]:
    stmt = select(Referral).order_by(Referral.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Referral.status == status)
    return list(session.scalars(stmt))


def resolve_referral(
    session: Session, referral_id: str, *, resolution: str, resolved_by: str, note: str | None
) -> Referral:
    ref = session.get(Referral, referral_id)
    if ref is None:
        raise ReferralNotFound(referral_id)
    if ref.status == ReferralStatus.RESOLVED:
        raise ReferralAlreadyResolved(referral_id)
    ref.status = ReferralStatus.RESOLVED
    ref.resolution = resolution
    ref.resolved_by = resolved_by
    ref.resolution_note = note
    ref.resolved_at = datetime.now(timezone.utc)
    session.commit()
    return ref
