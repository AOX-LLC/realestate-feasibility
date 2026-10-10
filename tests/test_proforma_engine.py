from datetime import date, timedelta
from decimal import Decimal

import pytest
from proforma_cases import (
    AS_OF,
    ASSUMPTIONS,
    S3_COMPS,
    TTL_DAYS,
    estimate,
    inputs,
    s1,
    s2,
    s3,
)

from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.engine import build_proforma, frozen_assumptions
from feasibility.proforma.model import Comp, EstimateInput, ProformaInputs, ProformaResult


def run(case: ProformaInputs) -> ProformaResult:
    return build_proforma(case, ASSUMPTIONS, estimate_ttl_days=TTL_DAYS)


def test_s1_is_computed_with_the_figures_of_the_formula_section() -> None:
    result = run(s1())

    assert (result.version, result.status, result.reason, result.flags) == (1, "computed", None, ())
    assert result.assumptions == frozen_assumptions(ASSUMPTIONS)
    assert result.site.rule_used == "R-7.5(A)"
    assert result.site.existing_sqft_source == "parcel"
    assert result.sizing is not None and result.sizing.buildable_sqft == 3168
    assert result.arv is not None
    assert result.arv.arv == Decimal("1420799.85")
    assert result.arv.median_psf == Decimal("448.4848")
    assert result.arv.estimate_stale is False
    assert result.costs is not None and result.costs.demolition == Decimal("11700.00")
    assert result.totals is not None
    assert result.totals.total_cost == Decimal("1313239.71")
    assert result.totals.profit == Decimal("107560.14")
    assert result.max_offer is not None
    assert result.max_offer.max_offer == Decimal("324271.50")
    assert result.max_offer.headroom_vs_offer == Decimal("-95728.50")
    assert result.sensitivity is not None and len(result.sensitivity.cells) == 60


def test_s2_vacant_land_has_no_demolition_and_is_capped() -> None:
    result = run(s2())

    assert result.status == "computed"
    assert result.flags == ("size_capped_max", "vacant_lot")
    assert result.site.existing_sqft_source == "vacant"
    assert result.costs is not None and result.costs.demolition == Decimal("0.00")
    assert result.totals is not None and result.totals.profit == Decimal("238226.56")
    assert result.max_offer is not None
    assert result.max_offer.max_offer == Decimal("313436.54")
    assert result.max_offer.headroom_vs_offer == Decimal("13436.54")


def test_s3_assumes_the_zoning_and_the_size_and_has_no_viable_offer() -> None:
    result = run(s3())

    assert result.status == "computed"
    assert result.flags == (
        "zoning_rule_assumed",
        "existing_area_assumed",
        "loss_exceeds_equity",
        "no_viable_offer",
    )
    assert result.site.rule_used == "default"
    assert result.site.existing_sqft_source == "assumed"
    assert result.costs is not None and result.costs.demolition == Decimal("14500.00")
    assert result.totals is not None and result.totals.profit == Decimal("-512840.11")
    assert result.max_offer is not None
    assert result.max_offer.max_offer is None
    assert result.max_offer.headroom_vs_offer is None


def test_a_vacant_lot_with_a_known_area_still_has_no_demolition() -> None:
    result = run(inputs(is_vacant=True, existing_living_sqft=Decimal("900")))

    assert result.costs is not None and result.costs.demolition == Decimal("0.00")
    assert "vacant_lot" in result.flags
    assert "existing_area_assumed" not in result.flags


@pytest.mark.parametrize("existing", [None, Decimal(0)])
def test_an_unknown_existing_area_uses_the_fallback_and_is_flagged(
    existing: Decimal | None,
) -> None:
    result = run(inputs(existing_living_sqft=existing))

    assert result.costs is not None and result.costs.demolition == Decimal("14500.00")
    assert "existing_area_assumed" in result.flags


def test_a_gis_group_is_flagged_and_demolishes_the_summed_area() -> None:
    result = run(inputs(is_gis_group=True, existing_living_sqft=Decimal("2300")))

    assert "gis_group" in result.flags
    assert result.site.is_gis_group is True
    assert result.costs is not None and result.costs.demolition == Decimal("20900.00")


def test_mixed_zoning_across_a_group_uses_the_default_rule_and_both_flags() -> None:
    result = run(
        inputs(is_gis_group=True, zoning="R-7.5(A)", zoning_values_seen=("R-7.5(A)", "CD-12"))
    )

    assert result.site.rule_used == "default"
    assert result.flags[:3] == ("gis_group", "zoning_mixed", "zoning_rule_assumed")


def test_the_same_zoning_spelled_two_ways_is_not_mixed() -> None:
    result = run(inputs(zoning_values_seen=("R-7.5(A)", "r-7.5 (a)")))

    assert "zoning_mixed" not in result.flags
    assert result.site.rule_used == "R-7.5(A)"


def test_no_estimate_yet_is_no_arv_but_keeps_the_costs_financing_and_holding() -> None:
    result = run(inputs(estimate=None))

    assert (result.status, result.reason) == ("no_arv", "no_estimate_yet")
    assert result.arv is None
    assert result.sizing is not None
    assert result.costs is not None
    assert result.financing is not None and result.financing.total == Decimal("74059.58")
    assert result.holding is not None and result.holding.total == Decimal("13785.74")
    assert result.selling is None
    assert result.totals is None
    assert result.max_offer is None
    assert result.sensitivity is None


def test_a_no_estimate_outcome_is_estimate_unavailable() -> None:
    result = run(inputs(estimate=estimate([], outcome="no_estimate")))

    assert (result.status, result.reason) == ("no_arv", "estimate_unavailable")
    assert result.arv is not None and result.arv.arv is None
    assert result.arv.estimate_fetched_on == date(2026, 10, 1)
    assert result.financing is not None and result.totals is None


def test_an_estimate_thirty_one_days_old_is_expired() -> None:
    result = run(inputs(estimate=estimate([(1, 1)], age_days=31)))

    assert (result.status, result.reason) == ("no_arv", "estimate_expired")
    assert result.arv is not None and result.arv.arv is None
    assert result.arv.estimate_stale is True
    assert result.arv.estimate_price == Decimal("400000")
    assert result.costs is not None and result.financing is not None


def test_an_estimate_thirty_days_old_is_still_used() -> None:
    result = run(
        inputs(estimate=estimate([(1330000, 3000), (1420000, 3150), (1250000, 2800)], age_days=30))
    )

    assert result.status == "computed"
    assert result.arv is not None and result.arv.estimate_stale is True


def test_an_estimate_older_than_the_ttl_is_used_and_flagged_stale() -> None:
    result = run(inputs(estimate=estimate(S3_COMPS, age_days=8)))

    assert result.status == "computed"
    assert result.arv is not None and result.arv.estimate_stale is True
    assert "estimate_stale" in result.flags


def test_an_estimate_exactly_at_the_ttl_is_fresh() -> None:
    result = run(inputs(estimate=estimate(S3_COMPS, age_days=TTL_DAYS)))

    assert result.arv is not None and result.arv.estimate_stale is False
    assert "estimate_stale" not in result.flags


def test_too_few_comps_is_no_arv_and_lists_the_comps_seen() -> None:
    result = run(inputs(estimate=estimate([(640000, 2300), (690000, 2500), (300000, 599)])))

    assert (result.status, result.reason) == ("no_arv", "too_few_comps")
    assert result.arv is not None
    assert (result.arv.comp_count_used, result.arv.comp_count_dropped) == (2, 1)
    assert [line.used for line in result.arv.comps] == [True, True, False]
    assert result.arv.arv is None
    assert result.financing is not None and result.totals is None


@pytest.mark.parametrize("lot", [None, Decimal(0)])
def test_a_missing_lot_is_unsizable_and_fills_only_the_site(lot: Decimal | None) -> None:
    result = run(inputs(lot_sqft=lot, is_gis_group=True))

    assert (result.status, result.reason) == ("unsizable", "lot_size_missing")
    assert result.flags == ("gis_group",)
    assert result.site.lot_sqft == lot
    assert result.site.rule_used is None
    assert result.assumptions == frozen_assumptions(ASSUMPTIONS)
    for section in (
        result.sizing,
        result.arv,
        result.costs,
        result.financing,
        result.holding,
        result.selling,
        result.totals,
        result.max_offer,
        result.sensitivity,
    ):
        assert section is None


def test_the_listing_lot_source_is_carried_to_the_site() -> None:
    assert run(inputs(lot_source="listing")).site.lot_source == "listing"


def test_a_lot_that_is_missing_is_reported_as_missing_on_the_unsizable_result() -> None:
    result = run(inputs(lot_sqft=None, lot_source="missing"))

    assert (result.status, result.reason) == ("unsizable", "lot_size_missing")
    assert result.site.lot_source == "missing"


def test_a_loss_smaller_than_the_cash_invested_is_not_flagged_as_exceeding_equity() -> None:
    result = run(inputs(price=Decimal("600000")))

    assert result.totals is not None
    assert result.totals.profit < 0
    assert result.totals.profit > -result.totals.cash_invested
    assert "loss_exceeds_equity" not in result.flags


def test_a_blank_zoning_among_the_values_seen_is_not_a_disagreement() -> None:
    result = run(inputs(is_gis_group=True, zoning_values_seen=("R-7.5(A)", "", "  ")))

    assert "zoning_mixed" not in result.flags
    assert result.site.rule_used == "R-7.5(A)"


def test_comps_that_price_the_house_at_nothing_are_no_arv() -> None:
    result = run(inputs(estimate=estimate([(1, 30000), (1, 30000), (1, 30000)])))

    assert (result.status, result.reason) == ("no_arv", "arv_not_positive")
    assert result.financing is not None and result.totals is None


def _zero_numerator_case() -> tuple[ProformaInputs, CostAssumptions]:
    """A deal built so that ARV x (1 - target) equals the fixed part of the cost exactly.

    No interest or points, a flat 2,000 sqft home, a 5% commission and no seller closing: the
    fixed part is 10,500.04 demolition + 380,000 hard + 19,000 contingency + 45,600 soft
    + 1,000 draw fees + 4,275 insurance + 5% of the ARV, and the ARV is 2,000 sqft at
    $287.7344, so 80% of the ARV is exactly 460,375.04.
    """
    zero = Decimal(0)
    assumptions = ASSUMPTIONS.model_copy(
        update={
            "demolition": ASSUMPTIONS.demolition.model_copy(update={"flat": Decimal("2500.04")}),
            "financing": ASSUMPTIONS.financing.model_copy(
                update={"rate_pct": zero, "points_pct": zero}
            ),
            "selling": ASSUMPTIONS.selling.model_copy(
                update={"commission_pct": Decimal(5), "closing_pct": zero}
            ),
            "sizing": ASSUMPTIONS.sizing.model_copy(
                update={"min_home_sqft": Decimal(2000), "max_home_sqft": Decimal(2000)}
            ),
        }
    )
    comps = tuple(
        Comp(
            address=f"{number} Comp St",
            price=Decimal("287734.40"),
            living_area_sqft=1000,
            distance_miles=None,
            year_built=None,
        )
        for number in range(1, 4)
    )
    fetched = EstimateInput(
        fetched_on=AS_OF - timedelta(days=1), outcome="ok", price=Decimal(400000), comps=comps
    )
    return inputs(existing_living_sqft=Decimal(1000), estimate=fetched), assumptions


def test_a_zero_numerator_pays_nothing_and_is_not_a_missing_offer() -> None:
    case, assumptions = _zero_numerator_case()

    result = build_proforma(case, assumptions, estimate_ttl_days=TTL_DAYS)

    assert result.arv is not None and result.arv.arv == Decimal("575468.80")
    assert result.max_offer is not None
    assert result.max_offer.fixed_part == Decimal("460375.04") + Decimal("28773.44")
    # The offer is zero dollars, which is an answer; only a negative numerator has none.
    assert result.max_offer.max_offer == Decimal("0.00")
    assert result.max_offer.headroom_vs_offer == -case.price
    assert "no_viable_offer" not in result.flags
