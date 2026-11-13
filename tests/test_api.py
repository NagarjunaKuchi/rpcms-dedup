from fastapi.testclient import TestClient

from rpcms_dedup.api import create_app

EVENT = {
    "idempotency_key": "key-api-0001",
    "progres_id": "UGA-00423318",
    "assistance_type": "cash-for-food",
    "cycle": "2026-03",
    "partner_id": "NHU",
    "record_id": "DST-NHU-0305-00214",
    "recorded_at": "2026-03-05T11:42:00Z",
    "amount": 150000,
    "currency": "UGX",
}


def test_requires_api_key(settings, engine):
    anon = TestClient(create_app(settings, engine))
    assert anon.get("/healthz").status_code == 200
    assert anon.post("/v1/assistance-events", json=EVENT).status_code == 401
    bad = TestClient(create_app(settings, engine), headers={"X-API-Key": "wrong-wrong-wrong"})
    assert bad.post("/v1/duplicate-checks", json={}).status_code == 401


def test_demo_story_mary_then_duplicate(client):
    chk = {k: EVENT[k] for k in ("progres_id", "assistance_type", "cycle", "partner_id")}
    assert client.post("/v1/duplicate-checks", json=chk).json()["duplicate"] is False

    r = client.post("/v1/assistance-events", json=EVENT)
    assert r.status_code == 201 and r.json()["outcome"] == "RECORDED"

    other = {**chk, "partner_id": "WNRP"}
    c = client.post("/v1/duplicate-checks", json=other).json()
    assert c["duplicate"] is True and c["existing"]["partner_id"] == "NHU"
    assert c["existing"]["record_id"] == "DST-NHU-0305-00214" and c["referral_id"]

    dup = client.post(
        "/v1/assistance-events",
        json={**EVENT, "idempotency_key": "key-api-0002", "partner_id": "WNRP", "record_id": "DST-WNRP-1"},
    )
    assert dup.status_code == 409 and dup.json()["code"] == "DUPLICATE_ASSISTANCE"


def test_replay_and_key_reuse(client):
    assert client.post("/v1/assistance-events", json=EVENT).status_code == 201
    assert client.post("/v1/assistance-events", json=EVENT).status_code == 200
    clash = client.post("/v1/assistance-events", json={**EVENT, "amount": 1})
    assert clash.status_code == 409 and clash.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_offline_conflict_and_referral_workflow(client):
    client.post("/v1/assistance-events", json=EVENT)
    off = client.post(
        "/v1/assistance-events",
        json={**EVENT, "idempotency_key": "key-api-0003", "partner_id": "WNRP", "record_id": "R9", "offline": True},
    )
    assert off.status_code == 202 and off.json()["outcome"] == "CONFLICT"
    open_refs = client.get("/v1/referrals", params={"status": "OPEN"}).json()
    assert len(open_refs) == 1 and open_refs[0]["reason"] == "OFFLINE_SYNC_CONFLICT"
    rid = open_refs[0]["id"]
    done = client.post(f"/v1/referrals/{rid}/resolve", json={"resolution": "CONFIRMED_DUPLICATE", "resolved_by": "cmdt-1"})
    assert done.status_code == 200 and done.json()["status"] == "RESOLVED"
    assert client.post(f"/v1/referrals/{rid}/resolve", json={"resolution": "FALSE_POSITIVE", "resolved_by": "x"}).status_code == 409
    assert client.post("/v1/referrals/missing/resolve", json={"resolution": "FALSE_POSITIVE", "resolved_by": "x"}).status_code == 404
    assert client.get("/v1/referrals", params={"status": "OPEN"}).json() == []


def test_validation(client):
    assert client.post("/v1/assistance-events", json={**EVENT, "currency": "UGANDA"}).status_code == 422
    assert client.post("/v1/assistance-events", json={**EVENT, "amount": -5}).status_code == 422
    assert client.get("/v1/referrals", params={"status": "BOGUS"}).status_code == 422
