"""How big a house the lot allows, from the zoning rule and the pack's size limits."""

from dataclasses import dataclass
from decimal import Decimal

from feasibility.markets.schema import Sizing, ZoningRule, normalise_zoning
from feasibility.proforma.model import CappedBy, SizingResult
from feasibility.proforma.money import round_sqft

DEFAULT_RULE = "default"
PERCENT = Decimal(100)


@dataclass(frozen=True)
class SizingChoice:
    sizing: SizingResult
    rule_used: str  # the pack's name for the zoning rule, or "default"
    flags: tuple[str, ...]


def buildable_size(lot_sqft: Decimal, zoning: str | None, config: Sizing) -> SizingChoice:
    """Choose the zoning rule, apply it to the lot, and clamp the result to the pack's range."""
    rule_used, rule = _choose_rule(zoning, config)
    footprint = lot_sqft * rule.coverage_pct / PERCENT
    gross = footprint * rule.stories
    uncapped = gross * rule.living_share_pct / PERCENT
    clamped = min(max(uncapped, config.min_home_sqft), config.max_home_sqft)

    flags: list[str] = []
    if rule_used == DEFAULT_RULE:
        flags.append("zoning_rule_assumed")
    capped_by: CappedBy = "none"
    if uncapped > config.max_home_sqft:
        capped_by = "max"
        flags.append("size_capped_max")
    elif uncapped < config.min_home_sqft:
        capped_by = "min"
        flags.append("size_capped_min")

    sizing = SizingResult(
        coverage_pct=rule.coverage_pct,
        stories=rule.stories,
        living_share_pct=rule.living_share_pct,
        footprint_sqft=footprint,
        gross_sqft=gross,
        uncapped_sqft=uncapped,
        min_home_sqft=config.min_home_sqft,
        max_home_sqft=config.max_home_sqft,
        buildable_sqft=round_sqft(clamped),
        capped_by=capped_by,
    )
    return SizingChoice(sizing=sizing, rule_used=rule_used, flags=tuple(flags))


def _choose_rule(zoning: str | None, config: Sizing) -> tuple[str, ZoningRule]:
    wanted = normalise_zoning(zoning or "")
    for name, rule in config.rules.items():
        if wanted and normalise_zoning(name) == wanted:
            return name, rule
    return DEFAULT_RULE, config.default
