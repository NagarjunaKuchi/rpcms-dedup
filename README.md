# rpcms-dedup: RPCMS Duplicate Guard

Cross-partner duplicate-assistance check and assistance ledger for the Refugee Partnership
Coordination & Monitoring System (RPCMS), built alongside OpenG2P Gen2.

Before a partner hands over assistance, RPCMS asks: has this household already received
the **same type of assistance** in the **same cycle** from **anyone**? This service answers
that question and records the handover atomically, so two partners cannot both win.

## How it works

- The caller sends a UNHCR ProGres ID. The service turns it into a keyed hash
  (`HMAC-SHA256(key, normalised ID)`), the *token*. **The raw ID is never stored.**
- One assistance event per `(token, assistance type, cycle)` holds the *dedup slot*, a unique
  column in the database. The database, not application code, decides the winner.
- Every write carries an idempotency key, so retries (including concurrent ones) are safe.
- Losers are never silently dropped. They are stored and a *referral* is opened for the
  settlement commandant:
  - online commit refused: `REJECTED_DUPLICATE` plus a referral;
  - offline record that lost after sync: kept as `CONFLICT` (the handover really happened) plus a referral.

## API (all but `/healthz` need an `X-API-Key` header)

| Method and path | Purpose | Responses |
| --- | --- | --- |
| `POST /v1/duplicate-checks` | Read-side check before handover. Opens a referral on a hit unless `open_referral=false`. | 200 |
| `POST /v1/assistance-events` | Atomic commit of a handover. | 201 recorded, 200 replay, 202 offline conflict stored, 409 `DUPLICATE_ASSISTANCE` or `IDEMPOTENCY_KEY_REUSED` |
| `GET /v1/referrals?status=OPEN` | Referral queue. | 200 |
| `POST /v1/referrals/{id}/resolve` | Resolve as `CONFIRMED_DUPLICATE` or `FALSE_POSITIVE`. | 200, 404, 409 |
| `GET /healthz` | Liveness. | 200 |

## Run

```bash
pip install -e ".[test]"             # add ".[postgres]" for PostgreSQL
export RPCMS_DEDUP_HMAC_KEY="$(openssl rand -hex 32)"   # keep this secret and stable
export RPCMS_DEDUP_API_KEY="$(openssl rand -hex 16)"
export RPCMS_DEDUP_DATABASE_URL="postgresql+psycopg://user:pass@host/rpcms_dedup"  # default: local SQLite
uvicorn rpcms_dedup.api:app_factory --factory --port 8000
pytest
```

## Known limits (read before production)

- **Key management.** The token is only as stable as `RPCMS_DEDUP_HMAC_KEY`. Rotating it
  orphans existing records; plan a re-keying procedure before you need one.
- **Schema.** Tables are created at start-up for development. Production needs migrations
  (for example Alembic), which are not included yet.
- **Offline gap.** A handover made offline can only be detected after sync (it is stored as
  `CONFLICT`). It is not prevented.
- **Authentication** is a single shared API key. Replace with mTLS or OAuth2 client
  credentials before exposing beyond a private network.
- Tested against SQLite only, including an 8-thread race. The same unique constraint
  should behave the same on PostgreSQL, but that is not yet verified.
- No licence file is included; add the one you intend to use.

Design context: see the RPCMS Technical and Architecture Specification (sections 1.3, 4b, 4c).
