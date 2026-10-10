"""The HTML of a printed pro-forma: golden files, escaping, and nothing it could fetch."""

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from markupsafe import escape

from feasibility.delivery.document import ProformaDocument
from feasibility.delivery.html import TEMPLATES, render_html

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "brief"
NAMES = ["s1-accepted", "s3-rejected", "gis-deferred"]


def _generated() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location(
        "build_brief_fixtures", REPO / "scripts" / "build_brief_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated: dict[str, str] = module.generate()
    return generated


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    return _generated()


def document(name: str) -> ProformaDocument:
    return ProformaDocument.model_validate_json((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("suffix", ["json", "html"])
def test_a_golden_file_is_what_the_generator_writes(
    generated: dict[str, str], name: str, suffix: str
) -> None:
    committed = (FIXTURES / f"{name}.{suffix}").read_text(encoding="utf-8")

    assert generated[f"{name}.{suffix}"] == committed


def test_the_three_fixtures_cover_an_accepted_a_withheld_and_a_deferred_narrative() -> None:
    assert {document(n).narrative_status for n in NAMES} == {
        "accepted",
        "withheld",
        "not_available",
    }


@pytest.mark.parametrize(
    "hostile",
    [
        "<img src=x onerror=1>",
        "{{7*7}}",
        "{% include 'x' %}",
        "<script>alert(1)</script>",
        '"><svg onload=1>',
        "</style><style>@import url(http://evil.example)</style>",
    ],
)
def test_text_in_any_field_is_rendered_as_text(hostile: str) -> None:
    base = document("s1-accepted")
    changed = base.model_copy(
        update={
            "street": hostile,
            "summary": hostile,
            "checks": [hostile],
            "flags": [type(base.flags[0])(code="a_code", meaning=hostile)] if base.flags else [],
        }
    )

    html = render_html(changed)

    # The text is there, escaped, as the characters it is: nothing in it was evaluated or parsed.
    assert str(escape(hostile)) in html
    assert "<img" not in html and "<script" not in html and "<svg" not in html
    assert html.count("<style") == 0


@pytest.mark.parametrize("name", NAMES)
def test_no_output_names_a_remote_address(name: str, generated: dict[str, str]) -> None:
    for text in (generated[f"{name}.html"], generated[f"{name}.json"]):
        assert "http://" not in text and "https://" not in text


def test_the_footer_sentence_is_in_the_page_rule_once_and_not_in_the_body() -> None:
    css = (TEMPLATES / "brief.css").read_text(encoding="utf-8")
    sentence = "Every cost value is illustrative, not a builder's actuals."

    assert css.count(sentence) == 1
    assert "@bottom-left" in css.split(sentence)[0].rsplit("@page", 1)[1]
    assert sentence not in render_html(document("s1-accepted")).replace("&#39;", "'")


def test_the_stylesheets_use_only_the_tokens_for_colour_and_type() -> None:
    css = (TEMPLATES / "brief.css").read_text(encoding="utf-8")

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.findall(r"\b\d+(?:\.\d+)?pt\b.*font-family", css)


def test_a_document_has_no_field_for_a_comp_address_and_none_reaches_it() -> None:
    from attack_support import leaves

    for name in NAMES:
        found: Any = json.loads((FIXTURES / f"{name}.json").read_text())
        keys = {key for key in _keys(found)}
        assert "address" not in keys and "quote" not in keys and "remarks" not in keys
        assert not [leaf for leaf in leaves(found) if isinstance(leaf, str) and "COMP ST" in leaf]


def _keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [*node, *[k for value in node.values() for k in _keys(value)]]
    if isinstance(node, list):
        return [k for value in node for k in _keys(value)]
    return []


def test_a_comp_address_in_the_stored_result_never_reaches_the_document() -> None:
    from decimal import Decimal

    import proforma_cases as pc

    from feasibility.delivery.brief import Brief, NotShown, TextContext
    from feasibility.delivery.build import make_candidate
    from feasibility.delivery.document import proforma_document
    from feasibility.proforma.engine import build_proforma

    base = pc.s1()
    marked = tuple(
        comp.model_copy(update={"address": f"{n} QUILLFEATHER AVE, DALLAS, TX 75209"})
        for n, comp in enumerate(base.estimate.comps if base.estimate else (), start=100)
    )
    inputs = pc.inputs(estimate=base.estimate.model_copy(update={"comps": marked}))
    result = build_proforma(inputs, pc.ASSUMPTIONS, estimate_ttl_days=pc.TTL_DAYS)
    assert any("QUILLFEATHER" in line.address for line in (result.arv.comps if result.arv else ()))
    entry = make_candidate(
        candidate_id=1,
        rank=1,
        score=Decimal("50"),
        street="1 TEST ST",
        zip5="75209",
        offer_price=inputs.price,
        result=result,
        signals=None,
        narrative=None,
        context=TextContext(),
    )
    brief = Brief(
        market="dallas",
        as_of=pc.AS_OF,
        run_id=1,
        data_mode="mock",
        completeness="complete",
        notice=None,
        ranked=1,
        shown=1,
        not_shown=NotShown(no_arv=0, unsizable=0, over_the_cap=0, no_pro_forma=0),
        candidates=[entry],
    )

    doc = proforma_document(brief, entry, result)

    assert "QUILLFEATHER" not in doc.model_dump_json()
    assert "QUILLFEATHER" not in render_html(doc)
