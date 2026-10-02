"""The provider response cache. Bodies are stored scrubbed; params hold query parameters
only, never credentials."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class CachedBody:
    body: Any
    fetched_at: datetime


def read(
    connection: Connection, provider: str, key: str, *, include_expired: bool = False
) -> CachedBody | None:
    row = connection.execute(
        text(
            """
            SELECT body, fetched_at FROM api_cache
            WHERE provider = :provider AND request_key = :key
              AND (:include_expired OR expires_at > now())
            """
        ),
        {"provider": provider, "key": key, "include_expired": include_expired},
    ).first()
    return None if row is None else CachedBody(row.body, row.fetched_at)


def store(
    connection: Connection,
    provider: str,
    key: str,
    *,
    endpoint: str,
    params: dict[str, str],
    body: Any,
    ttl: timedelta,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO api_cache (provider, request_key, endpoint, params, body, expires_at)
            VALUES (:provider, :key, :endpoint, CAST(:params AS jsonb), CAST(:body AS jsonb),
                    now() + :ttl)
            ON CONFLICT (provider, request_key) DO UPDATE
            SET body = EXCLUDED.body, params = EXCLUDED.params, fetched_at = now(),
                expires_at = EXCLUDED.expires_at
            """
        ),
        {
            "provider": provider,
            "key": key,
            "endpoint": endpoint,
            "params": json.dumps(params),
            "body": json.dumps(body),
            "ttl": ttl,
        },
    )
