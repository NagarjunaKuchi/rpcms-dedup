from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class EventStatus:
    RECORDED = "RECORDED"  # first assistance for (token, type, cycle); holds the dedup slot
    CONFLICT = "CONFLICT"  # offline record that lost the race; kept, flagged for review
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"  # online commit refused as a duplicate; kept for audit


class AssistanceEvent(Base):
    """One assistance handover recorded by a partner.

    Only the RECORDED event of a (token, assistance_type, cycle) holds ``dedup_slot``.
    The unique constraint on that column is what makes the duplicate check atomic:
    two concurrent commits cannot both succeed. NULLs do not collide, so CONFLICT and
    REJECTED_DUPLICATE events (slot NULL) are stored without blocking anything.
    """

    __tablename__ = "assistance_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistance_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_slot: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)

    partner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    auth_txn_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    officer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


Index("ix_event_slot_lookup", AssistanceEvent.token, AssistanceEvent.assistance_type, AssistanceEvent.cycle)


class ReferralStatus:
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ReferralReason:
    ONLINE_CHECK = "ONLINE_DUPLICATE_CHECK"
    COMMIT_REFUSED = "COMMIT_REFUSED_DUPLICATE"
    OFFLINE_SYNC = "OFFLINE_SYNC_CONFLICT"


class Referral(Base):
    """A duplicate flagged for the settlement commandant to review."""

    __tablename__ = "referral"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistance_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle: Mapped[str] = mapped_column(String(64), nullable=False)
    requesting_partner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_event_id: Mapped[str] = mapped_column(ForeignKey("assistance_event.id"), nullable=False)
    conflicting_event_id: Mapped[str | None] = mapped_column(ForeignKey("assistance_event.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ReferralStatus.OPEN)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
