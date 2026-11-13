from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from RPCMS_DEDUP_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RPCMS_DEDUP_", env_file=".env", extra="ignore")

    # SQLAlchemy URL. Use PostgreSQL (postgresql+psycopg://...) outside development.
    database_url: str = "sqlite:///./rpcms_dedup.db"

    # Secret for the keyed hash that turns a ProGres ID into the duplicate token.
    # Raw ProGres IDs are never stored. Losing or rotating this key breaks matching
    # against existing records, so manage it like any other long-lived secret.
    hmac_key: str = Field(min_length=32)

    # Shared secret that callers (Bridge connector, field sync workers) send in X-API-Key.
    api_key: str = Field(min_length=16)
