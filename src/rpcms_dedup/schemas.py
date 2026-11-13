from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

ProgresId = Annotated[str, Field(min_length=3, max_length=64)]
Code = Annotated[str, Field(min_length=1, max_length=64)]


class DuplicateCheckRequest(BaseModel):
    progres_id: ProgresId
    assistance_type: Code
    cycle: Code
    partner_id: Code
    open_referral: bool = True


class ExistingAssistance(BaseModel):
    event_id: str
    partner_id: str
    record_id: str
    recorded_at: datetime
    amount: float | None = None
    currency: str | None = None


class DuplicateCheckResponse(BaseModel):
    duplicate: bool
    token: str
    existing: ExistingAssistance | None = None
    referral_id: str | None = None


class AssistanceEventRequest(BaseModel):
    idempotency_key: Annotated[str, Field(min_length=8, max_length=128)]
    progres_id: ProgresId
    assistance_type: Code
    cycle: Code
    partner_id: Code
    record_id: Annotated[str, Field(min_length=1, max_length=128)]
    recorded_at: datetime
    amount: Annotated[float, Field(ge=0)] | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    auth_txn_ref: str | None = Field(default=None, max_length=128)
    officer_id: str | None = Field(default=None, max_length=64)
    offline: bool = False


class AssistanceEventResponse(BaseModel):
    outcome: Literal["RECORDED", "REPLAY", "DUPLICATE", "CONFLICT"]
    event_id: str
    status: str
    existing: ExistingAssistance | None = None
    referral_id: str | None = None


class ReferralOut(BaseModel):
    id: str
    reason: str
    assistance_type: str
    cycle: str
    requesting_partner_id: str
    primary_event_id: str
    conflicting_event_id: str | None
    status: str
    resolution: str | None
    created_at: datetime


class ResolveReferralRequest(BaseModel):
    resolution: Literal["CONFIRMED_DUPLICATE", "FALSE_POSITIVE"]
    resolved_by: Code
    note: str | None = Field(default=None, max_length=1000)


class ErrorBody(BaseModel):
    code: str
    detail: str
