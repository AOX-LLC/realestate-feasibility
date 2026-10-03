"""After-repair value: the median price per square foot of the sale comps, times the size built."""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from feasibility.markets.schema import ArvRules
from feasibility.proforma.model import Comp, CompLine
from feasibility.proforma.money import round_money, round_ratio

PERCENT = Decimal(100)
TOO_FEW_COMPS = "too_few_comps"
ARV_NOT_POSITIVE = "arv_not_positive"


@dataclass(frozen=True)
class PricedArv:
    comps: tuple[CompLine, ...]
    comp_count_used: int
    comp_count_dropped: int
    median_psf: Decimal
    premium_pct: Decimal
    arv: Decimal


@dataclass(frozen=True)
class ArvUnavailable:
    reason: str
    comps: tuple[CompLine, ...] = ()
    comp_count_used: int = 0
    comp_count_dropped: int = 0


def price_arv(
    comps: Sequence[Comp], buildable_sqft: Decimal, config: ArvRules
) -> PricedArv | ArvUnavailable:
    """Price the house to be built from the comps that are big enough to compare."""
    lines = tuple(_comp_line(comp, config.min_comp_sqft) for comp in comps)
    used = [line.psf for line in lines if line.used and line.psf is not None]
    dropped = len(lines) - len(used)
    if len(used) < config.min_comps:
        return ArvUnavailable(
            TOO_FEW_COMPS, comps=lines, comp_count_used=len(used), comp_count_dropped=dropped
        )

    median_psf = statistics.median(used)
    premium = 1 + config.new_build_premium_pct / PERCENT
    arv = round_money(median_psf * buildable_sqft * premium)
    if arv <= 0:  # comps priced at a few cents a foot: nothing to divide a margin by
        return ArvUnavailable(
            ARV_NOT_POSITIVE, comps=lines, comp_count_used=len(used), comp_count_dropped=dropped
        )
    return PricedArv(
        comps=lines,
        comp_count_used=len(used),
        comp_count_dropped=dropped,
        median_psf=median_psf,
        premium_pct=config.new_build_premium_pct,
        arv=arv,
    )


def _comp_line(comp: Comp, min_comp_sqft: Decimal) -> CompLine:
    area = comp.living_area_sqft
    usable = area is not None and comp.price > 0 and area >= min_comp_sqft
    psf = round_ratio(comp.price / area) if usable and area is not None else None
    return CompLine(
        address=comp.address,
        price=comp.price,
        living_area_sqft=area,
        psf=psf,
        used=usable,
    )
