"""The PDF of a printed pro-forma, tested structurally (the HTML has golden files)."""

import io
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from test_delivery_html import FIXTURES, NAMES, document

from feasibility.delivery.html import FONTS, TEMPLATES
from feasibility.delivery.pdf import allowed_path, render_pdf, safe_fetcher

FOOTER = "Every cost value is illustrative, not a builder's actuals."


@pytest.fixture(scope="module")
def rendered() -> dict[str, PdfReader]:
    return {name: PdfReader(io.BytesIO(render_pdf(document(name)))) for name in NAMES}


def text_of(reader: PdfReader) -> list[str]:
    return [page.extract_text() for page in reader.pages]


@pytest.mark.parametrize("name", NAMES)
def test_every_page_has_the_footer_and_a_page_count(
    rendered: dict[str, PdfReader], name: str
) -> None:
    pages = text_of(rendered[name])

    assert 1 <= len(pages) <= 3
    for number, page in enumerate(pages, start=1):
        assert FOOTER in page.replace(chr(0x2019), "'")
        assert f"Page {number} of {len(pages)}" in page


@pytest.mark.parametrize("name", NAMES)
def test_every_figure_string_of_the_document_is_in_the_text(
    rendered: dict[str, PdfReader], name: str
) -> None:
    doc = document(name)
    text = " ".join(" ".join(text_of(rendered[name])).split())
    shown = [
        line.value for line in (*doc.key_figures, *doc.cost_stack, *doc.sizing, *doc.arv_basis)
    ]
    shown += [cell for row in doc.grid_rows for cell in row.cells] + [doc.list_price, doc.score]

    missing = [value for value in shown if value not in text]

    assert missing == []


def test_a_withheld_narrative_shows_the_note_and_no_draft_text(
    rendered: dict[str, PdfReader],
) -> None:
    doc = document("s3-rejected")
    text = " ".join(text_of(rendered["s3-rejected"]))

    assert doc.narrative_status == "withheld"
    assert doc.narrative_note in " ".join(text.split())
    assert "108k" not in text and doc.summary == "" and not doc.risks


def test_a_deferred_narrative_says_so_and_the_accepted_one_is_labelled(
    rendered: dict[str, PdfReader],
) -> None:
    deferred = " ".join(" ".join(text_of(rendered["gis-deferred"])).split())
    accepted = " ".join(" ".join(text_of(rendered["s1-accepted"])).split())

    assert "Not available today." in deferred
    assert "Written by a language model. Every figure in it was checked" in accepted


def test_no_comp_address_is_in_any_pdf(rendered: dict[str, PdfReader]) -> None:
    for reader in rendered.values():
        text = " ".join(text_of(reader))
        assert "COMP ST" not in text and "QUILLFEATHER" not in text


def test_the_fonts_are_the_token_families(rendered: dict[str, PdfReader]) -> None:
    names: set[str] = set()
    for reader in rendered.values():
        for page in reader.pages:
            fonts = page["/Resources"].get("/Font", {})
            for font in fonts.values():
                names.add(str(font.get_object()["/BaseFont"]))

    joined = " ".join(names).replace("-", "")
    assert "SpaceGrotesk" in joined and "IBMPlexSans" in joined and "IBMPlexMono" in joined


def test_the_title_and_dates_come_from_the_document(rendered: dict[str, PdfReader]) -> None:
    doc = document("s1-accepted")
    meta = rendered["s1-accepted"].metadata

    assert meta is not None
    assert str(meta.title) == f"Pro-forma #{doc.rank} {doc.street}, {doc.zip5}"
    assert "2026-10-02" in str(meta.creation_date)


def test_two_renders_have_the_same_text() -> None:
    first = PdfReader(io.BytesIO(render_pdf(document("s1-accepted"))))
    second = PdfReader(io.BytesIO(render_pdf(document("s1-accepted"))))

    assert text_of(first) == text_of(second)


def test_the_pdf_has_no_link_annotations(rendered: dict[str, PdfReader]) -> None:
    for reader in rendered.values():
        for page in reader.pages:
            for annotation in page.get("/Annots", []) or []:
                assert "/URI" not in str(annotation.get_object())
                assert "/Launch" not in str(annotation.get_object())


# --- the fetcher -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9/x",
        "https://example.com/f.woff",
        "file:///etc/passwd",
        "file:///tmp/anything.css",
        "data:text/css,body{color:red}",
        "ftp://example.com/x",
        f"file://{TEMPLATES}/../../../../../etc/passwd",
        f"file://{TEMPLATES}/missing.css",
    ],
)
def test_the_fetcher_refuses_everything_but_the_packages_own_files(url: str) -> None:
    from weasyprint.urls import FatalURLFetchingError

    with pytest.raises(FatalURLFetchingError):
        safe_fetcher().fetch(url)


def test_the_fetcher_serves_a_packaged_stylesheet_and_font() -> None:
    fetcher = safe_fetcher()
    css = fetcher.fetch((TEMPLATES / "tokens.css").as_uri())
    font = fetcher.fetch((FONTS / "IBMPlexSans" / "IBMPlexSans-Regular.ttf").as_uri())

    assert b"--ink" in css.read()
    assert len(font.read()) > 100_000


def test_a_link_out_of_the_folders_by_a_symlink_is_refused(tmp_path: Path) -> None:
    link = TEMPLATES / "zz-test-link.css"
    secret = tmp_path / "secret.css"
    secret.write_text("body {}")
    try:
        link.symlink_to(secret)
        with pytest.raises(ValueError):
            allowed_path(link.as_uri())
    finally:
        link.unlink(missing_ok=True)


def test_a_render_needs_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a pro-forma render opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    assert render_pdf(document("s1-accepted")).startswith(b"%PDF")


def test_importing_the_api_does_not_load_weasyprint() -> None:
    code = (
        "import sys; import feasibility.api.app; "
        "print([m for m in sys.modules if m.split('.')[0] == 'weasyprint'])"
    )

    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout

    assert out.strip() == "[]"


def test_the_fixtures_exist() -> None:
    assert sorted(p.name for p in FIXTURES.glob("*.json")) == [f"{n}.json" for n in sorted(NAMES)]
