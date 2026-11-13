import pytest
from fastapi.testclient import TestClient

from rpcms_dedup.api import create_app
from rpcms_dedup.config import Settings
from rpcms_dedup.db import init_schema, make_engine, make_session_factory

HMAC_KEY = "k" * 40
API_KEY = "test-api-key-123456"


@pytest.fixture()
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path}/t.db", hmac_key=HMAC_KEY, api_key=API_KEY)


@pytest.fixture()
def engine(settings):
    e = make_engine(settings.database_url)
    init_schema(e)
    return e


@pytest.fixture()
def session(engine):
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def client(settings, engine):
    return TestClient(create_app(settings, engine), headers={"X-API-Key": API_KEY})
