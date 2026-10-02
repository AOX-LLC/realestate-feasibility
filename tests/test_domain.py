import pytest

from feasibility.domain import Address, normalize_street, zip5
from feasibility.sources.base import ListingQuery
from feasibility.sources.mls.stub import MlsListingSource, NotConfiguredError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 123  North Elm Street. ", "123 N ELM ST"),
        ("4500 s. lamar blvd", "4500 S LAMAR BLVD"),
        ("77 Bluebonnet Trail #4", "77 BLUEBONNET TRL #4"),
        ("", ""),
    ],
)
def test_normalize_street(raw: str, expected: str) -> None:
    assert normalize_street(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("752141234", "75214"),
        ("75214-1234", "75214"),
        (" 75214 ", "75214"),
        ("7521", None),
        (None, None),
    ],
)
def test_zip5(raw: str | None, expected: str | None) -> None:
    assert zip5(raw) == expected


def test_two_spellings_of_one_address_compare_equal() -> None:
    from_listing = Address.normalized(
        "123 North Elm Street", city="Dallas", state="tx", zip_code="75214"
    )
    from_parcel = Address.normalized(
        "123 N ELM ST", city="DALLAS", state="TX", zip_code="752141234"
    )

    assert from_listing == from_parcel
    assert from_listing.one_line == "123 N ELM ST, DALLAS, TX 75214"


def test_mls_stub_explains_it_is_not_configured() -> None:
    query = ListingQuery(city="Dallas", state="TX", days_old=1, limit=10)

    with pytest.raises(NotConfiguredError, match="no MLS feed"):
        MlsListingSource().fetch_listings(query)
