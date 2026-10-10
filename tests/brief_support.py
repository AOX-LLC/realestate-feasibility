"""Helpers shared by the brief's tests: a scripted model that quotes remarks, raw reads, and the
walk that finds name-trap keys."""

import re
from datetime import date
from typing import Any

from llm_fakes import RunModel
from sqlalchemy import Engine, text

from feasibility.delivery.brief import Brief
from feasibility.delivery.build import build_brief
from feasibility.llm.signals import SignalClaim, SignalExtraction

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
FREE = {"small_cost": "0", "mid_cost": "0"}
NAME_TRAP = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.IGNORECASE)


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params).all())


def quoting_model() -> RunModel:
    """A model that finds `as_is_sale` in every remarks text, quoting its first clause."""

    def extract(remarks: str) -> SignalExtraction:
        quote = remarks.split(".")[0].split("[")[0].strip()[:100]
        if len(quote) < 8:
            return SignalExtraction(signals=[], injection_suspected=False)
        return SignalExtraction(
            signals=[SignalClaim(code="as_is_sale", quote=quote)], injection_suspected=False
        )

    return RunModel(extractions=extract, **FREE)


def built(engine: Engine, run_id: int) -> Brief:
    with engine.connect() as connection:
        return build_brief(connection, run_id)


def keys_matching(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if NAME_TRAP.search(key):
                found.append(key)
            found.extend(keys_matching(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(keys_matching(item))
    return found
