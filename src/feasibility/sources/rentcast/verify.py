"""A small live check of the RentCast API against the models: at most four calls, with a
hard ceiling of ten, spending from the same monthly budget. The local report records field
names and value types only, never values."""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr
from sqlalchemy import Engine

from feasibility.config import Settings
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import RentCastListings
from feasibility.sources.base import ListingQuery
from feasibility.sources.rentcast import budget
from feasibility.sources.rentcast.client import RentCastClient, Ttls
from feasibility.sources.rentcast.transport import HttpTransport, Transport, TransportResponse

CALL_CEILING = 10
DATE_LIKE_KEY = re.compile(r"^\d{4}(-\d{2}-\d{2})?$")


class CallCeilingError(RuntimeError):
    pass


class CeilingTransport:
    """Refuses to send more than `ceiling` requests."""

    def __init__(self, inner: Transport, ceiling: int = CALL_CEILING) -> None:
        self._inner = inner
        self._ceiling = ceiling
        self.calls = 0

    def get(self, path: str, params: Mapping[str, str]) -> TransportResponse:
        if self.calls >= self._ceiling:
            raise CallCeilingError(
                f"refusing call {self.calls + 1}; the ceiling is {self._ceiling}"
            )
        self.calls += 1
        return self._inner.get(path, params)

    def close(self) -> None:
        self._inner.close()


def field_shapes(value: Any, path: str = "") -> dict[str, str]:
    """Field path -> JSON type. Date- and year-keyed maps collapse to '*'."""
    if isinstance(value, dict):
        shapes: dict[str, str] = {}
        for key, item in value.items():
            name = "*" if DATE_LIKE_KEY.match(str(key)) else str(key)
            shapes.update(field_shapes(item, f"{path}.{name}" if path else name))
        return shapes
    if isinstance(value, list):
        shapes = {}
        for item in value:
            shapes.update(field_shapes(item, f"{path}[]"))
        return shapes
    return {path: type(value).__name__}


def _dump(model: BaseModel | list[Any] | None) -> Any:
    if model is None:
        return None
    if isinstance(model, list):
        return [_dump(item) for item in model]
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True)


def verify(engine: Engine, settings: Settings) -> Path:
    """Check the live API once. Takes the spend lock first and does not wait for it: a check
    that spends from the monthly budget must not run beside the daily run that counts it."""
    if not settings.is_live or settings.rentcast_api_key is None:
        raise RuntimeError("verify-rentcast needs DATA_MODE=live and RENTCAST_API_KEY")
    with budget.spend_lock(engine, wait=False):
        return _verify(engine, settings, settings.rentcast_api_key)


def _verify(engine: Engine, settings: Settings, api_key: SecretStr) -> Path:
    spec = next(
        s for s in get_pack(settings.market).sources.listings if isinstance(s, RentCastListings)
    )
    transport = CeilingTransport(HttpTransport(api_key))
    client = RentCastClient(
        engine,
        transport,
        live=True,
        monthly_budget=settings.rentcast_monthly_budget,
        billing_anchor_day=settings.rentcast_billing_anchor_day,
        ttls=Ttls(
            settings.rentcast_ttl_sale_listings,
            settings.rentcast_ttl_property_records,
            settings.rentcast_ttl_value_estimates,
        ),
        use_cache=False,
    )
    query = ListingQuery(city=spec.city, state=spec.state, days_old=spec.days_old, limit=1)
    shapes: dict[str, dict[str, str]] = {}
    try:
        listings = client.sale_listings(query).data
        shapes["/listings/sale"] = field_shapes(_dump(listings))
        if listings:
            first = listings[0]
            shapes["/listings/sale/{id}"] = field_shapes(_dump(client.sale_listing(first.id).data))
            address = first.formatted_address
            shapes["/properties"] = field_shapes(_dump(client.property_record(address).data))
            shapes["/avm/value"] = field_shapes(_dump(client.value_estimate(address).data))
    finally:
        client.close()

    checked_at = datetime.now(UTC)
    settings.local_dir.mkdir(parents=True, exist_ok=True)
    report_path = settings.local_dir / f"rentcast-verify-{checked_at:%Y%m%dT%H%M%SZ}.json"
    report = {"checked_at": checked_at.isoformat(), "calls": transport.calls, "shapes": shapes}
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path
