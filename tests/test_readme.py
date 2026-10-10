"""The README's front page and the proof kit: links resolve, the claims are the scorecards', the
mock renderings are labelled, and the capture script writes only where it may."""

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
EVALS = REPO / "docs" / "EVALS.md"
MEDIA = REPO / "docs" / "media"
SCORECARDS = REPO / "evals" / "scorecards"
CAPTURE = REPO / "scripts" / "capture" / "capture_proof.py"
STAGE = REPO / "scripts" / "capture" / "stage.html"
MAX_MEDIA_BYTES = 3_000_000
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _links(path: Path) -> list[str]:
    return LINK.findall(path.read_text(encoding="utf-8"))


def _local(target: str) -> bool:
    return not target.startswith(("http://", "https://", "#", "mailto:"))


@pytest.mark.parametrize("doc", [README, EVALS, MEDIA / "README.md"], ids=lambda p: p.name)
def test_every_relative_link_and_image_resolves(doc: Path) -> None:
    broken = [
        target
        for target in _links(doc)
        if _local(target) and not (doc.parent / target.split("#")[0]).exists()
    ]

    assert broken == []


def test_the_front_page_images_are_committed_small_and_real_files() -> None:
    images = [t for t in _links(README) if t.startswith("docs/media/") and not t.endswith(".md")]

    assert sorted(set(images)) == [
        "docs/media/morning-brief.gif",
        "docs/media/notion-table-mock.png",
        "docs/media/pro-forma-page-1.png",
        "docs/media/slack-digest-mock.png",
    ]
    for image in set(images):
        data = (REPO / image).read_bytes()
        assert len(data) <= MAX_MEDIA_BYTES, image
        assert data[:8] == b"\x89PNG\r\n\x1a\n" or data[:6] in (b"GIF87a", b"GIF89a"), image


def test_every_file_in_docs_media_is_described_in_its_readme() -> None:
    described = (MEDIA / "README.md").read_text(encoding="utf-8")

    for path in MEDIA.iterdir():
        if path.name != "README.md":
            assert path.name in described, path.name


def test_the_mock_renderings_are_labelled_in_the_readme_and_in_the_image_template() -> None:
    text = README.read_text(encoding="utf-8")
    for match in re.finditer(r"!\[([^\]]*)\]\((docs/media/[^)]*mock[^)]*)\)", text):
        assert "mock" in match.group(1).lower(), match.group(2)
    assert "This is a mock rendering" in text

    stage = STAGE.read_text(encoding="utf-8")
    assert "MOCK RENDERING" in stage
    assert "Not a screenshot of Slack." in stage
    assert "Not a screenshot of Notion." in stage


def test_the_frozen_snapshot_statement_is_on_the_front_page() -> None:
    top = README.read_text(encoding="utf-8")[:4000]

    assert "The demo runs on a frozen synthetic snapshot." in top
    assert "Live RentCast has not been called" in top
    assert "real county appraisal (DCAD) archive has not been imported" in top
    assert "No message has been sent to a real Notion or Slack" in top


def _front_page() -> str:
    text = README.read_text(encoding="utf-8")
    return text[: text.index("\n## Status")]


def _summary(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((SCORECARDS / name).read_text("utf-8"))["summary"]


def test_the_numbers_on_the_front_page_are_the_scorecards() -> None:
    top = _front_page()
    holdout, dev = _summary("signals-holdout.json"), _summary("signals-dev.json")
    narrative = _summary("narrative.json")
    teardown = next(r for r in holdout["per_signal"] if r["code"] == "teardown_language")

    assert f"precision {holdout['micro_precision'] * 100:.1f}%" in top
    assert f"recall {holdout['micro_recall'] * 100:.0f}%" in top
    assert f"evidence match {holdout['evidence_match'] * 100:.0f}%" in top
    assert (
        f"injection resisted {holdout['injection_passed']} of {holdout['injection_cases']}" in top
    )
    assert f"({holdout['cases_scored']} records)" in top
    assert f"precision {dev['micro_precision'] * 100:.1f}%" in top
    assert f"({dev['cases_scored']} records)" in top
    assert f"({dev['injection_passed']} of {dev['injection_cases']})" in top
    assert f"accepted {narrative['accepted']} of {narrative['cases_scored']}" in top
    assert (
        f"{teardown['false_positives']} false positives against {teardown['true_positives']}" in top
    )
    assert f"(precision {teardown['precision'] * 100:.0f}%)" in top


def test_the_personal_data_claim_carries_the_residual_cases_it_depends_on() -> None:
    holdout = _summary("signals-holdout.json")
    assert holdout["personal_leaks"] == {}  # the gated claim is true...
    assert holdout["residual_leaks"]  # ...and the residual forms did leak, so say so

    for text in (_front_page(), README.read_text(encoding="utf-8")):
        for line in text.splitlines():
            if "no personal data leaked" in line:
                assert "residual" in line, line
                assert f"{len(holdout['residual_leaks'])} of {holdout['residual_cases']}" in line


def test_the_front_page_cost_speed_and_test_counts_are_the_reports_and_the_measurement() -> None:
    top = _front_page()
    report = EVALS.read_text(encoding="utf-8")
    stack = json.loads((SCORECARDS / "stack-run.json").read_text("utf-8"))
    narrative = _summary("narrative.json")
    total = re.search(r"\*\*All `test_proforma_\*\.py`\*\* \| \*\*(\d+)\*\*", report)
    both_days = re.search(r"\*\*Both days\*\* \| \| \*\*(\d+)\*\* .* \*\*\$([\d.]+)\*\*", report)
    seconds = [d["trigger_to_delivered_seconds"] for d in stack["days"].values()]

    assert total is not None
    assert both_days is not None
    assert f"{total.group(1)} tests" in top
    cost = f"${float(both_days.group(2)):.4f} in model calls ({both_days.group(1)} recorded calls)"
    assert cost in top
    assert f"about ${5 * float(narrative['cost_per_case_usd']):.2f}" in top
    assert f"{min(seconds):.1f} to {max(seconds):.1f} seconds" in top


# --- the capture script -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_proof", CAPTURE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # imports no browser: those imports are inside the commands
    return module


def test_the_capture_script_refuses_to_write_outside_the_media_folders(
    capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEDIA_OUT", raising=False)
    monkeypatch.delenv("MEDIA_OUT_DIR", raising=False)

    for forbidden in (tmp_path / "x", REPO / "src", REPO / "docs", Path("/tmp"), REPO):  # noqa: S108
        with pytest.raises(capture.CaptureError):
            capture.safe_out(forbidden)

    monkeypatch.setenv("MEDIA_OUT", str(tmp_path))
    assert capture.safe_out(tmp_path / "05-proof-kit") == (tmp_path / "05-proof-kit").resolve()
    # A path that only starts like an allowed one is still outside it.
    with pytest.raises(capture.CaptureError):
        capture.safe_out(Path(str(tmp_path) + "-other"))


def test_the_live_capture_commands_exit_with_a_message_and_capture_nothing(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("live-slack", "live-notion"):
        monkeypatch.setattr(sys, "argv", ["capture_proof.py", command])
        assert capture.main() == 2

    err = capsys.readouterr().err
    assert "docs/media/slack-digest-live.png" in err
    assert "docs/media/notion-table-live.png" in err


def test_the_capture_script_needs_the_read_token_and_never_prints_it(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_READ_TOKEN", raising=False)
    with pytest.raises(capture.CaptureError, match="API_READ_TOKEN is not set"):
        capture.check_stack("http://127.0.0.1:1", "2026-10-02")

    source = CAPTURE.read_text(encoding="utf-8")
    assert not re.search(r"print\([^)]*token", source, re.IGNORECASE)
