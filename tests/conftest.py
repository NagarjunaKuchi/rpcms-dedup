import os
import pytest
from fastapi.testclient import TestClient

from rpcms_dedup.api import create_app
from rpcms_dedup.config import Settings
from rpcms_dedup.db import init_schema, make_engine, make_session_factory

HMAC_KEY = "k" * 40
API_KEY = "test-api-key-123456"


@pytest.fixture()
def settings(tmp_path):
    url = os.environ.get("RPCMS_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    return Settings(database_url=url, hmac_key=HMAC_KEY, api_key=API_KEY, create_tables="RPCMS_TEST_DATABASE_URL" not in os.environ)


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


@pytest.fixture(autouse=True)
def _clean_external_db():
    """With RPCMS_TEST_DATABASE_URL set (a throwaway Postgres already migrated with `alembic upgrade head`),
    empty the tables between tests. The default per-test SQLite file needs nothing."""
    url = os.environ.get("RPCMS_TEST_DATABASE_URL")
    if url:
        from sqlalchemy import create_engine, text
        with create_engine(url).begin() as c:
            c.execute(text("TRUNCATE assistance_event, referral RESTART IDENTITY CASCADE"))
    yield
