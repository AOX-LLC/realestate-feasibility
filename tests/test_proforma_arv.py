from decimal import Decimal

from feasibility.markets.loader import get_pack
from feasibility.markets.schema import ArvRules
from feasibility.proforma.arv import ArvUnavailable, PricedArv, price_arv
from feasibility.proforma.model import Comp

RULES = get_pack("dallas").cost_assumptions.arv


def comp(price: int, area: int | None, address: str = "1 Main St") -> Comp:
    return Comp(
        address=address,
        price=Decimal(price),
        living_area_sqft=area,
        distance_miles=None,
        year_built=None,
    )


S1_COMPS = [
    comp(1330000, 3000),
    comp(1420000, 3150),
    comp(1250000, 2800),
    comp(1480000, 3300),
    comp(1190000, 2650),
    comp(1390000, 3100),
    comp(1520000, 3350),
]
S3_COMPS = [comp(640000, 2300), comp(690000, 2500), comp(610000, 2250), comp(720000, 2700)]


def priced(comps: list[Comp], buildable: str, rules: ArvRules = RULES) -> PricedArv:
    result = price_arv(comps, Decimal(buildable), rules)
    assert isinstance(result, PricedArv)
    return result


def test_an_odd_count_takes_the_middle_price_per_foot() -> None:
    result = priced(S1_COMPS, "3168")

    assert result.comp_count_used == 7
    assert result.comp_count_dropped == 0
    assert result.median_psf == Decimal("448.4848")
    assert result.arv == Decimal("1420799.85")


def test_an_even_count_averages_the_two_middle_values_without_rounding_again() -> None:
    result = priced(S3_COMPS, "2480")

    assert result.median_psf == Decimal("273.55555")
    assert result.arv == Decimal("678417.76")


def test_each_comp_psf_is_rounded_to_four_places() -> None:
    result = priced(S3_COMPS, "2480")

    assert [line.psf for line in result.comps] == [
        Decimal("278.2609"),
        Decimal("276.0000"),
        Decimal("271.1111"),
        Decimal("266.6667"),
    ]
    assert all(line.used for line in result.comps)


def test_a_comp_below_the_minimum_area_is_dropped_and_listed_unused() -> None:
    comps = [*S3_COMPS, comp(300000, 599, "Tiny St")]

    result = priced(comps, "2480")

    assert result.comp_count_used == 4
    assert result.comp_count_dropped == 1
    assert result.median_psf == Decimal("273.55555")
    tiny = result.comps[-1]
    assert (tiny.address, tiny.used, tiny.psf) == ("Tiny St", False, None)


def test_a_comp_at_exactly_the_minimum_area_is_used() -> None:
    result = priced([*S3_COMPS, comp(300000, 600)], "2480")

    assert result.comp_count_used == 5
    assert result.median_psf == Decimal("276.0000")  # 500.0000 is the new highest of five


def test_comps_with_no_area_or_no_price_are_dropped() -> None:
    comps = [*S3_COMPS, comp(500000, None, "No area"), comp(0, 2000, "No price")]

    result = priced(comps, "2480")

    assert (result.comp_count_used, result.comp_count_dropped) == (4, 2)
    assert [line.used for line in result.comps] == [True] * 4 + [False, False]


def test_fewer_than_the_minimum_usable_comps_is_unavailable() -> None:
    comps = [*S3_COMPS[:2], comp(300000, 599)]

    result = price_arv(comps, Decimal("2480"), RULES)

    assert isinstance(result, ArvUnavailable)
    assert result.reason == "too_few_comps"
    assert (result.comp_count_used, result.comp_count_dropped) == (2, 1)
    assert len(result.comps) == 3


def test_no_comps_at_all_is_unavailable() -> None:
    result = price_arv([], Decimal("2480"), RULES)

    assert result == ArvUnavailable("too_few_comps")


def test_the_new_build_premium_is_applied_once_to_the_unrounded_product() -> None:
    rules = RULES.model_copy(update={"new_build_premium_pct": Decimal("10")})

    result = priced(S1_COMPS, "3168", rules)

    # 448.4848 x 3168 x 1.10 = 1,562,879.83104; rounding the 10 %-less ARV first would give .84
    assert result.premium_pct == Decimal("10")
    assert result.arv == Decimal("1562879.83")


def test_the_input_order_does_not_change_the_answer() -> None:
    assert priced(S1_COMPS[::-1], "3168").arv == priced(S1_COMPS, "3168").arv
