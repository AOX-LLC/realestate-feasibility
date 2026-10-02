"""Mock mode finds a value estimate for each of the six estimate candidates, by the address
the client sends. Before the AVM fixtures were re-keyed on that address, every lookup missed."""

from typing import NamedTuple

import pytest
from sqlalchemy import Engine

from feasibility.config import DataMode, Settings
from feasibility.domain.address import Address
from feasibility.sources.rentcast.adapter import RentCastValuationSource
from feasibility.sources.rentcast.client import RentCastClient


class Case(NamedTuple):
    account: str
    street: str
    zip_code: str
    overlay: str | None


# Accounts 051, 004, 052, 002 and 006 are priced on day 1; 015 first appears on day 2, under
# the listing's spelling (its parcel is renamed in the current CAD set).
CASES = [
    Case("051", "8773 Orrinmoor Trl", "75209", None),
    Case("004", "1893 Thistlewane Dr", "75218", None),
    Case("052", "2680 Valdermere Trl", "75218", None),
    Case("002", "5284 Ostravelle Ln", "75206", None),
    Case("006", "6515 Elderquist Ave", "75223", None),
    Case("015", "554 Ostravelle Ave", "75228", "day-2"),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.account)
def test_the_mock_client_finds_the_estimate_for_each_estimate_address(
    engine: Engine, case: Case
) -> None:
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    client = RentCastClient.from_settings(engine, settings, snapshot_day=case.overlay)
    address = Address.normalized(case.street, city="Dallas", state="TX", zip_code=case.zip_code)

    estimate = RentCastValuationSource(client).estimate_value(address)
    client.close()

    assert address.one_line == f"{case.street.upper()}, DALLAS, TX {case.zip_code}"
    assert estimate is not None
    assert estimate.dropped_comparables == 2
    assert len(estimate.comparables) == 5
    assert all(comp.living_area_sqft is not None for comp in estimate.comparables)


def test_an_address_with_no_fixture_has_no_estimate(engine: Engine) -> None:
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    client = RentCastClient.from_settings(engine, settings)
    address = Address.normalized("1 Nowhere St", city="Dallas", state="TX", zip_code="75209")

    assert RentCastValuationSource(client).estimate_value(address) is None
    client.close()


def test_the_title_case_address_the_fixtures_used_to_be_keyed_on_misses(engine: Engine) -> None:
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    client = RentCastClient.from_settings(engine, settings)

    assert client.value_estimate("8773 Orrinmoor Trl, Dallas, TX 75209").data is None
    client.close()
