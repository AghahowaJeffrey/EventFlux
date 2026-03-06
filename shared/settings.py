"""
EventFlux — Shared Settings

Reads environment variables and provides typed config objects
for both the ingestion API and the batch worker.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    db: str = "eventflux"
    user: str = "eventflux"
    password: str = "eventflux_secret"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db}"
        )

    @property
    def async_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db}"
        )

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")


class RedisSettings(BaseSettings):
    host: str = "localhost"
    port: int = 6379
    stream_key: str = "events:raw"
    consumer_group: str = "batch_workers"

    @property
    def url(self) -> str:
        return f"redis://{self.host}:{self.port}/0"

    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")


class APISettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    max_batch_size: int = 5000

    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore")


class WorkerSettings(BaseSettings):
    batch_size: int = 500
    flush_interval_s: float = 2.0
    max_block_ms: int = 2000
    single_row_mode: bool = False  # Experiment: use executemany instead of COPY

    model_config = SettingsConfigDict(env_prefix="WORKER_", extra="ignore")


class AggregationSettings(BaseSettings):
    interval_s: int = 60

    model_config = SettingsConfigDict(env_prefix="AGGREGATION_", extra="ignore")


@lru_cache
def get_postgres() -> PostgresSettings:
    return PostgresSettings()


@lru_cache
def get_redis() -> RedisSettings:
    return RedisSettings()


@lru_cache
def get_api() -> APISettings:
    return APISettings()


@lru_cache
def get_worker() -> WorkerSettings:
    return WorkerSettings()


@lru_cache
def get_aggregation() -> AggregationSettings:
    return AggregationSettings()
