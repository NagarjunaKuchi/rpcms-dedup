from __future__ import annotations

import hmac
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import __version__, service
from .config import Settings
from .db import init_schema, make_engine, make_session_factory
from .models import AssistanceEvent
from .schemas import (
    AssistanceEventRequest,
    AssistanceEventResponse,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    ErrorBody,
    ExistingAssistance,
    ReferralOut,
    ResolveReferralRequest,
)


def _existing(ev: AssistanceEvent | None) -> ExistingAssistance | None:
    if ev is None:
        return None
    return ExistingAssistance(
        event_id=ev.id,
        partner_id=ev.partner_id,
        record_id=ev.record_id,
        recorded_at=ev.recorded_at,
        amount=None if ev.amount is None else float(ev.amount),
        currency=ev.currency,
    )


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = engine or make_engine(settings.database_url)
    if settings.create_tables:
        init_schema(engine)  # dev/tests only; production uses Alembic (see README)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="RPCMS Duplicate Guard", version=__version__)

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def require_api_key(x_api_key: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_api_key.encode(), settings.api_key.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    auth = [Depends(require_api_key)]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/duplicate-checks", response_model=DuplicateCheckResponse, dependencies=auth)
    def duplicate_check(body: DuplicateCheckRequest, session: Session = Depends(get_session)):
        result = service.check_duplicate(
            session,
            settings.hmac_key,
            progres_id=body.progres_id,
            assistance_type=body.assistance_type,
            cycle=body.cycle,
            partner_id=body.partner_id,
            open_referral=body.open_referral,
        )
        return DuplicateCheckResponse(
            duplicate=result.duplicate,
            token=result.token,
            existing=_existing(result.existing),
            referral_id=result.referral.id if result.referral else None,
        )

    @app.post(
        "/v1/assistance-events",
        response_model=AssistanceEventResponse,
        responses={409: {"model": ErrorBody}},
        dependencies=auth,
    )
    def record_event(body: AssistanceEventRequest, session: Session = Depends(get_session)):
        req = service.RecordRequest(**body.model_dump())
        try:
            result = service.record_assistance(session, settings.hmac_key, req)
        except service.IdempotencyKeyReused:
            return JSONResponse(
                status_code=409,
                content=ErrorBody(
                    code="IDEMPOTENCY_KEY_REUSED",
                    detail="This idempotency key was already used with a different payload.",
                ).model_dump(),
            )
        payload = AssistanceEventResponse(
            outcome=result.outcome,
            event_id=result.event.id,
            status=result.event.status,
            existing=_existing(result.primary),
            referral_id=result.referral.id if result.referral else None,
        )
        status = {
            service.Outcome.RECORDED: 201,
            service.Outcome.REPLAY: 200,
            service.Outcome.CONFLICT: 202,
            service.Outcome.DUPLICATE: 409,
        }[result.outcome]
        if result.outcome == service.Outcome.DUPLICATE:
            content = payload.model_dump(mode="json")
            content["code"] = "DUPLICATE_ASSISTANCE"
            return JSONResponse(status_code=409, content=content)
        return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))

    @app.get("/v1/referrals", response_model=list[ReferralOut], dependencies=auth)
    def referrals(
        status: str | None = Query(default=None, pattern="^(OPEN|RESOLVED)$"),
        limit: int = Query(default=100, ge=1, le=500),
        session: Session = Depends(get_session),
    ):
        return [
            ReferralOut(
                id=r.id,
                reason=r.reason,
                assistance_type=r.assistance_type,
                cycle=r.cycle,
                requesting_partner_id=r.requesting_partner_id,
                primary_event_id=r.primary_event_id,
                conflicting_event_id=r.conflicting_event_id,
                status=r.status,
                resolution=r.resolution,
                created_at=r.created_at,
            )
            for r in service.list_referrals(session, status, limit)
        ]

    @app.post("/v1/referrals/{referral_id}/resolve", response_model=ReferralOut, dependencies=auth)
    def resolve(referral_id: str, body: ResolveReferralRequest, session: Session = Depends(get_session)):
        try:
            r = service.resolve_referral(
                session,
                referral_id,
                resolution=body.resolution,
                resolved_by=body.resolved_by,
                note=body.note,
            )
        except service.ReferralNotFound:
            raise HTTPException(status_code=404, detail="Referral not found")
        except service.ReferralAlreadyResolved:
            raise HTTPException(status_code=409, detail="Referral already resolved")
        return ReferralOut(
            id=r.id,
            reason=r.reason,
            assistance_type=r.assistance_type,
            cycle=r.cycle,
            requesting_partner_id=r.requesting_partner_id,
            primary_event_id=r.primary_event_id,
            conflicting_event_id=r.conflicting_event_id,
            status=r.status,
            resolution=r.resolution,
            created_at=r.created_at,
        )

    return app


def app_factory() -> FastAPI:  # for: uvicorn rpcms_dedup.api:app_factory --factory
    return create_app()
