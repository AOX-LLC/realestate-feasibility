"""The cost assumptions as a stored result holds them, frozen.

`ProformaResult.assumptions` is stored with every pro-forma (it is the "what was assumed" of that
number), and a stored result is read back through its model. Typed as the market pack's own
`CostAssumptions`, any change to the pack schema (a new required field, a renamed one) would make
every stored result unreadable, or silently change what it says. These classes are verbatim copies
of the pack's models as they stood when stored results were first written (version 1 of
`ProformaResult`). The pack may change freely; the engine converts the pack's assumptions into this
shape when it builds a result, and a change to this file is a new version of the result.

A new required field in the pack is read by nothing here (stored results stay readable), but the
conversion refuses a pack that has fields these classes do not (`extra="forbid"`), so building a new
pro-forma fails until a v2 exists: the pack and this file change together, on purpose.

Do not edit the fields. To change what a result records, add `assumptions_v2.py`, a new
`ProformaResult` version and a reader for both.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Percent = Annotated[Decimal, Field(ge=0, le=100)]
Money = Annotated[Decimal, Field(ge=0)]
Sqft = Annotated[Decimal, Field(gt=0)]


class FrozenV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AcquisitionV1(FrozenV1):
    closing_pct: Percent


class DemolitionV1(FrozenV1):
    flat: Money
    per_sqft: Money
    fallback_sqft: Sqft


class ConstructionV1(FrozenV1):
    hard_cost_per_sqft: Annotated[Decimal, Field(gt=0)]
    contingency_pct: Percent
    soft_cost_pct: Percent
    build_share_pct: Annotated[Decimal, Field(gt=0, le=100)]


class FinancingV1(FrozenV1):
    loan_to_cost_pct: Percent
    rate_pct: Annotated[Decimal, Field(ge=0, le=40)]
    points_pct: Percent
    draw_count: Annotated[int, Field(ge=0)]
    draw_fee: Money


class HoldingV1(FrozenV1):
    hold_months: Annotated[Decimal, Field(gt=0)]
    property_tax_rate_pct: Annotated[Decimal, Field(ge=0, le=10)]
    insurance_pct_of_hard_cost_per_year: Percent


class SellingV1(FrozenV1):
    commission_pct: Percent
    closing_pct: Percent


class TargetV1(FrozenV1):
    margin_pct: Percent


class ArvRulesV1(FrozenV1):
    min_comps: Annotated[int, Field(ge=1)]
    min_comp_sqft: Sqft
    new_build_premium_pct: Percent
    estimate_max_age_days: Annotated[int, Field(ge=1)]


class ZoningRuleV1(FrozenV1):
    coverage_pct: Annotated[Decimal, Field(gt=0, le=100)]
    stories: Annotated[int, Field(ge=1, le=10)]
    living_share_pct: Annotated[Decimal, Field(gt=0, le=100)]


class SizingV1(FrozenV1):
    min_home_sqft: Sqft
    max_home_sqft: Sqft
    default: ZoningRuleV1
    rules: dict[str, ZoningRuleV1]

    @field_validator("rules")
    @classmethod
    def _zoning_keys_are_unique_once_normalised(
        cls, rules: dict[str, ZoningRuleV1]
    ) -> dict[str, ZoningRuleV1]:
        normalised = ["".join(key.split()).upper() for key in rules]
        if len(set(normalised)) != len(normalised):
            raise ValueError("zoning rules repeat a zoning after upper-casing and removing spaces")
        return rules

    @model_validator(mode="after")
    def _check_home_size_range(self) -> "SizingV1":
        if self.min_home_sqft > self.max_home_sqft:
            raise ValueError("min_home_sqft must not exceed max_home_sqft")
        return self


class SensitivityV1(FrozenV1):
    arv_delta_pct: list[Annotated[Decimal, Field(gt=-100)]] = Field(min_length=1)
    hard_cost_delta_pct: list[Annotated[Decimal, Field(gt=-100)]] = Field(min_length=1)
    hold_months: list[Annotated[Decimal, Field(gt=0)]] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_grid_has_a_base_case(self) -> "SensitivityV1":
        for name in ("arv_delta_pct", "hard_cost_delta_pct", "hold_months"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not repeat a value")
        if Decimal(0) not in self.arv_delta_pct:
            raise ValueError("arv_delta_pct must contain 0")
        if Decimal(0) not in self.hard_cost_delta_pct:
            raise ValueError("hard_cost_delta_pct must contain 0")
        return self


class CostAssumptionsV1(FrozenV1):
    """Inputs to the pro-forma; `status` says whether they are illustrative or reviewed."""

    status: Literal["illustrative", "reviewed"]
    sources_read_on: date
    acquisition: AcquisitionV1
    demolition: DemolitionV1
    construction: ConstructionV1
    financing: FinancingV1
    holding: HoldingV1
    selling: SellingV1
    target: TargetV1
    arv: ArvRulesV1
    sizing: SizingV1
    sensitivity: SensitivityV1

    @model_validator(mode="after")
    def _check_grid_contains_the_base_hold(self) -> "CostAssumptionsV1":
        if self.holding.hold_months not in self.sensitivity.hold_months:
            raise ValueError("sensitivity.hold_months must contain holding.hold_months")
        return self
