"""The RentCast client: one request path for both modes, with a response cache, a hard
monthly budget and a log row for every attempt.

Request path (live):
    canonical key -> fresh cache hit? -> advisory lock on the key -> cache again
    -> reserve a budget unit (committed before the call) -> send -> scrub -> validate
    -> cache and log.
Mock mode answers from the committed snapshot after the cache check and never touches
the budget.

The client never retries; jobs retry with backoff. Errors carry the endpoint, the status
and RentCast's short error code only, never the request, its headers or the message.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine, text

from feasibility.config import Settings
from feasibility.sources.base import ListingQuery
from feasibility.sources.rentcast import budget, cache
from feasibility.sources.rentcast.models import PropertyRecord, SaleListing, ValueEstimate
from feasibility.sources.rentcast.scrub import scrub
from feasibility.sources.rentcast.transport import (
    ConnectFailedError,
    HttpTransport,
    NoResponseError,
    SnapshotTransport,
    Transport,
    canonical_params,
    request_key,
)

log = logging.getLogger(__name__)

PROVIDER = "rentcast"
ERROR_CODE = re.compile(r"^[A-Za-z0-9/_.-]{1,64}$")

SALE_LISTINGS = TypeAdapter(list[SaleListing])
SALE_LISTING = TypeAdapter(SaleListing)
PROPERTY_RECORDS = TypeAdapter(list[PropertyRecord])
VALUE_ESTIMATE = TypeAdapter(ValueEstimate)


def sale_listings_params(query: ListingQuery) -> dict[str, str]:
    """Query parameters for GET /listings/sale; the snapshot generator keys on these too."""
    return canonical_params(
        {
            "city": query.city,
            "state": query.state,
            "status": query.status,
            "daysOld": str(query.days_old),
            "limit": str(query.limit),
        }
    )


def sale_listing_path(listing_id: str) -> str:
    return f"/listings/sale/{quote(listing_id, safe='')}"


def property_record_params(address: str) -> dict[str, str]:
    return canonical_params({"address": address, "limit": "1"})


def value_estimate_params(address: str, comp_count: int = 15) -> dict[str, str]:
    return canonical_params({"address": address, "compCount": str(comp_count)})


class RentCastError(Exception):
    def __init__(self, endpoint: str, status: int | None, error_code: str | None) -> None:
        detail = f"status {status}" if status is not None else "request failed"
        super().__init__(f"RentCast {endpoint} failed: {detail} {error_code or ''}".rstrip())
        self.endpoint = endpoint
        self.status = status
        self.error_code = error_code


class BudgetExhaustedError(Exception):
    def __init__(self, period_start: date, limit: int) -> None:
        super().__init__(
            f"RentCast budget of {limit} requests for the period from {period_start} is spent"
        )


class SchemaDriftError(Exception):
    """RentCast answered 200 with a body the models reject: the API's shape changed."""

    def __init__(self, endpoint: str, field_paths: list[str]) -> None:
        super().__init__(
            f"RentCast {endpoint} response no longer matches the models: {field_paths}"
        )
        self.field_paths = field_paths


@dataclass(frozen=True)
class Fetched[T]:
    data: T
    # True when the request failed and an expired cache entry was served instead.
    stale: bool = False


@dataclass(frozen=True)
class Ttls:
    sale_listings: timedelta
    property_records: timedelta
    value_estimates: timedelta


@dataclass(frozen=True)
class _Request:
    endpoint: str
    path: str
    params: dict[str, str]
    key: str
    ttl: timedelta


class RentCastClient:
    def __init__(
        self,
        engine: Engine,
        transport: Transport,
        *,
        live: bool,
        monthly_budget: int,
        billing_anchor_day: int,
        ttls: Ttls,
        use_cache: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._transport = transport
        self._live = live
        self._monthly_budget = monthly_budget
        self._anchor_day = billing_anchor_day
        self._ttls = ttls
        self._use_cache = use_cache
        self._clock = clock

    @classmethod
    def from_settings(
        cls, engine: Engine, settings: Settings, *, use_cache: bool = True
    ) -> "RentCastClient":
        transport: Transport
        if settings.is_live and settings.rentcast_api_key is not None:
            transport = HttpTransport(settings.rentcast_api_key)
        else:
            transport = SnapshotTransport(settings.snapshot_dir / "rentcast")
        return cls(
            engine,
            transport,
            live=settings.is_live,
            monthly_budget=settings.rentcast_monthly_budget,
            billing_anchor_day=settings.rentcast_billing_anchor_day,
            ttls=Ttls(
                sale_listings=settings.rentcast_ttl_sale_listings,
                property_records=settings.rentcast_ttl_property_records,
                value_estimates=settings.rentcast_ttl_value_estimates,
            ),
            use_cache=use_cache,
        )

    def close(self) -> None:
        self._transport.close()

    def budget_usage(self) -> budget.BudgetUsage:
        period = budget.period_start(self._clock().date(), self._anchor_day)
        with self._engine.connect() as connection:
            return budget.usage(connection, PROVIDER, period, self._monthly_budget)

    def sale_listings(self, query: ListingQuery) -> Fetched[list[SaleListing]]:
        request = self._request(
            "/listings/sale",
            "/listings/sale",
            sale_listings_params(query),
            self._ttls.sale_listings,
        )
        fetched = self._get(request, SALE_LISTINGS)
        return Fetched(fetched.data or [], fetched.stale)

    def sale_listing(self, listing_id: str) -> Fetched[SaleListing | None]:
        request = self._request(
            "/listings/sale/{id}", sale_listing_path(listing_id), {}, self._ttls.sale_listings
        )
        return self._get(request, SALE_LISTING)

    def property_record(self, address: str) -> Fetched[PropertyRecord | None]:
        params = property_record_params(address)
        request = self._request("/properties", "/properties", params, self._ttls.property_records)
        fetched = self._get(request, PROPERTY_RECORDS)
        records = fetched.data or []
        return Fetched(records[0] if records else None, fetched.stale)

    def value_estimate(self, address: str, comp_count: int = 15) -> Fetched[ValueEstimate | None]:
        params = value_estimate_params(address, comp_count)
        request = self._request("/avm/value", "/avm/value", params, self._ttls.value_estimates)
        return self._get(request, VALUE_ESTIMATE)

    def _request(
        self, endpoint: str, path: str, params: Mapping[str, str], ttl: timedelta
    ) -> _Request:
        canonical = canonical_params(params)
        return _Request(endpoint, path, canonical, request_key(path, canonical), ttl)

    def _get[T](self, request: _Request, adapter: TypeAdapter[T]) -> Fetched[T | None]:
        if self._use_cache:
            with self._engine.begin() as connection:
                hit = self._cached(connection, request, adapter)
            if hit is not None:
                return hit
        if not self._live:
            return self._from_snapshot(request, adapter)

        with self._engine.connect() as connection:
            # Concurrent callers for the same request wait here, then find it cached,
            # so only one of them pays.
            connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": request.key}
            )
            connection.commit()
            try:
                return self._fetch_live(connection, request, adapter)
            finally:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": request.key},
                )
                connection.commit()

    def _cached[T](
        self, connection: Connection, request: _Request, adapter: TypeAdapter[T]
    ) -> Fetched[T | None] | None:
        hit = cache.read(connection, PROVIDER, request.key)
        if hit is None:
            return None
        self._log(connection, request, None, "cache_hit", None, billed=False)
        return Fetched(adapter.validate_python(hit.body))

    def _from_snapshot[T](self, request: _Request, adapter: TypeAdapter[T]) -> Fetched[T | None]:
        response = self._transport.get(request.path, request.params)
        with self._engine.begin() as connection:
            if response.status_code == 200:
                data = self._validate(connection, request, None, scrub(response.body), adapter)
                self._log(connection, request, None, "ok", 200, billed=False)
                return Fetched(data)
            outcome = "not_found" if response.status_code == 404 else "http_error"
            self._log(connection, request, None, outcome, response.status_code, billed=False)
        if response.status_code == 404:
            return Fetched(None)
        raise RentCastError(request.endpoint, response.status_code, _error_code(response.body))

    def _fetch_live[T](
        self, connection: Connection, request: _Request, adapter: TypeAdapter[T]
    ) -> Fetched[T | None]:
        if self._use_cache:
            hit = self._cached(connection, request, adapter)
            connection.commit()
            if hit is not None:
                return hit

        period = budget.period_start(self._clock().date(), self._anchor_day)
        if not budget.reserve(connection, PROVIDER, period, self._monthly_budget):
            self._log(connection, request, period, "refused_budget", None, billed=False)
            connection.commit()
            return self._stale_or_raise(
                connection, request, adapter, BudgetExhaustedError(period, self._monthly_budget)
            )
        # The reservation is durable before the call: a crash mid-call over-counts, never under.
        connection.commit()

        try:
            response = self._transport.get(request.path, request.params)
        except ConnectFailedError:
            budget.refund(connection, PROVIDER, period)
            self._log(connection, request, period, "network_error", None, billed=False)
            connection.commit()
            error = RentCastError(request.endpoint, None, "connect_failed")
            return self._stale_or_raise(connection, request, adapter, error)
        except NoResponseError:
            # Sent but unanswered: it may have been billed, so the unit stays spent.
            self._log(connection, request, period, "network_error", None, billed=True)
            connection.commit()
            error = RentCastError(request.endpoint, None, "no_response")
            return self._stale_or_raise(connection, request, adapter, error)

        if response.status_code == 200:
            body = scrub(response.body)
            data = self._validate(connection, request, period, body, adapter)
            cache.store(
                connection,
                PROVIDER,
                request.key,
                endpoint=request.endpoint,
                params=request.params,
                body=body,
                ttl=request.ttl,
            )
            self._log(connection, request, period, "ok", 200, billed=True)
            connection.commit()
            return Fetched(data)

        # RentCast does not bill requests that return an error.
        budget.refund(connection, PROVIDER, period)
        if response.status_code == 404:
            self._log(connection, request, period, "not_found", 404, billed=False)
            connection.commit()
            return Fetched(None)
        self._log(connection, request, period, "http_error", response.status_code, billed=False)
        connection.commit()
        error = RentCastError(request.endpoint, response.status_code, _error_code(response.body))
        return self._stale_or_raise(connection, request, adapter, error)

    def _validate[T](
        self,
        connection: Connection,
        request: _Request,
        period: date | None,
        body: Any,
        adapter: TypeAdapter[T],
    ) -> T:
        try:
            return adapter.validate_python(body)
        except ValidationError as error:
            field_paths = sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()})
            self._log(connection, request, period, "schema_error", 200, billed=self._live)
            connection.commit()
            log.warning("RentCast %s shape drift at %s", request.endpoint, field_paths)
            raise SchemaDriftError(request.endpoint, field_paths) from None

    def _stale_or_raise[T](
        self, connection: Connection, request: _Request, adapter: TypeAdapter[T], error: Exception
    ) -> Fetched[T | None]:
        if not self._use_cache:
            raise error
        stale = cache.read(connection, PROVIDER, request.key, include_expired=True)
        if stale is None:
            raise error
        self._log(connection, request, None, "stale_served", None, billed=False)
        connection.commit()
        log.warning("serving a stale RentCast %s response after: %s", request.endpoint, error)
        return Fetched(adapter.validate_python(stale.body), stale=True)

    def _log(
        self,
        connection: Connection,
        request: _Request,
        period: date | None,
        outcome: str,
        status_code: int | None,
        *,
        billed: bool,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO api_request_log (provider, endpoint, request_key, period_start,
                    outcome, status_code, billed)
                VALUES (:provider, :endpoint, :key, :period, :outcome, :status_code, :billed)
                """
            ),
            {
                "provider": PROVIDER,
                "endpoint": request.endpoint,
                "key": request.key,
                "period": period,
                "outcome": outcome,
                "status_code": status_code,
                "billed": billed,
            },
        )


def _error_code(body: Any) -> str | None:
    """RentCast's short error code (e.g. 'auth/api-key-invalid'). Its free-text message
    is dropped: it can echo request details."""
    if not isinstance(body, dict):
        return None
    code = body.get("error")
    return code if isinstance(code, str) and ERROR_CODE.match(code) else None
