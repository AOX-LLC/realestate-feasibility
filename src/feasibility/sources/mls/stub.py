"""Where a client's own MLS feed plugs in.

A real adapter reads RESO Data Dictionary Property resources (RESO Web API or a
replicated feed) and maps them onto domain.Listing:

    RESO field            domain.Listing field
    --------------------  ---------------------------------------------------------
    ListingKey            external_id
    UnparsedAddress       address (via Address.normalized, with City,
                          StateOrProvince, PostalCode)
    ListPrice             price
    StandardStatus        status ("Active", "Pending", "Closed", ...)
    PropertyType          property_type
    LotSizeSquareFeet     lot_size_sqft (or LotSizeAcres * 43,560)
    LivingArea            living_area_sqft
    YearBuilt             year_built
    ListingContractDate   listed_date
    PublicRemarks         remarks: the listing text the LLM layer reads

Agent and office fields (ListAgent*, ListOffice*) and any private remarks are
never mapped, matching the personal-data rule for every other source.
"""

from feasibility.sources.base import ListingBatch, ListingQuery, NotConfiguredError


class MlsListingSource:
    name = "mls"

    def fetch_listings(self, query: ListingQuery) -> ListingBatch:
        raise NotConfiguredError(
            "no MLS feed is configured; this adapter shows where a client's RESO feed plugs in"
        )
