"""scripts/capture/capture_proof.py: where it may write, what it refuses, and that the read token
goes only to a local API and never into a message. No browser is started: the browser and PDF
imports are inside the commands that need them."""

import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "scripts" / "capture" / "capture_proof.py"
SENTINEL = "tok-SENTINEL-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_proof", CAPTURE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_media_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_OUT", raising=False)
    monkeypatch.delenv("MEDIA_OUT_DIR", raising=False)


# --- where it may write ---------------------------------------------------------------------------


def test_it_refuses_to_write_outside_the_media_folder_and_local(
    capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for forbidden in (tmp_path / "x", REPO / "src", REPO / "docs", Path("/tmp"), REPO):  # noqa: S108
        with pytest.raises(capture.CaptureError):
            capture.safe_out(forbidden)

    monkeypatch.setenv("MEDIA_OUT", str(tmp_path))
    assert capture.safe_out(tmp_path / "proof-kit") == (tmp_path / "proof-kit").resolve()
    with pytest.raises(capture.CaptureError):  # a path that only starts like an allowed one
        capture.safe_out(Path(str(tmp_path) + "-other"))


def test_docs_media_is_the_destination_of_optimise_only(capture: ModuleType) -> None:
    with pytest.raises(capture.CaptureError):
        capture.safe_out(REPO / "docs" / "media")
    with pytest.raises(capture.CaptureError):
        capture.safe_out(REPO / "docs" / "media" / "sub", allow_docs_media=False)

    assert capture.safe_out(REPO / "docs" / "media", allow_docs_media=True) == (
        REPO / "docs" / "media"
    )


def test_the_stacks_own_outbox_is_not_a_place_for_captures(
    capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDIA_OUT_DIR", str(tmp_path))  # what the Compose stack writes to

    with pytest.raises(capture.CaptureError):
        capture.safe_out(tmp_path / "proof-kit")


def test_an_unwritable_output_folder_is_a_message_not_a_traceback(
    capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("MEDIA_OUT", str(tmp_path))
    try:
        with pytest.raises(capture.CaptureError, match="cannot create"):
            capture.safe_out(locked / "proof-kit")
    finally:
        locked.chmod(0o700)


def test_the_live_commands_exit_with_a_message_and_capture_nothing(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("live-slack", "live-notion"):
        monkeypatch.setattr(sys, "argv", ["capture_proof.py", command])
        assert capture.main() == 2

    err = capsys.readouterr().err
    assert "docs/media/slack-digest-live.png" in err
    assert "docs/media/notion-table-live.png" in err


# --- the token --------------------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    status = 200
    seen_auth: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        _Stub.seen_auth.append(self.headers.get("Authorization"))
        body = json.dumps({"status": "ok", "items": []}).encode()
        # /livez is open and always answers; the gated routes answer with the status under test.
        self.send_response(200 if self.path == "/livez" else _Stub.status)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub_api() -> Iterator[str]:
    _Stub.seen_auth = []
    _Stub.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_token_is_required(capture: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_READ_TOKEN", raising=False)

    with pytest.raises(capture.CaptureError, match="API_READ_TOKEN is not set"):
        capture.check_stack("http://127.0.0.1:1", "2026-10-02")


@pytest.mark.parametrize("status", [401, 500])
def test_no_message_or_output_of_a_failed_check_holds_the_token(
    capture: ModuleType,
    stub_api: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    monkeypatch.setenv("API_READ_TOKEN", SENTINEL)
    monkeypatch.setenv("MEDIA_OUT", str(tmp_path))
    _Stub.status = status
    monkeypatch.setattr(sys, "argv", ["capture_proof.py", "check", "--api", stub_api])

    assert capture.main() == 2

    seen = capsys.readouterr()
    assert SENTINEL not in seen.err + seen.out
    assert str(status) in seen.err


def test_an_unreachable_api_is_a_message_without_the_token(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_READ_TOKEN", SENTINEL)

    with pytest.raises(capture.CaptureError) as error:
        capture.check_stack("http://127.0.0.1:1", "2026-10-02")

    assert SENTINEL not in str(error.value)


def test_the_token_goes_only_to_a_local_api(
    capture: ModuleType, stub_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_READ_TOKEN", SENTINEL)
    for remote in ("https://example.com", "http://example.com:4501", "file:///etc/passwd"):
        with pytest.raises(capture.CaptureError, match="--api must be"):
            capture.check_stack(remote, "2026-10-02")

    with pytest.raises(capture.CaptureError, match="no run for"):  # a local stub is accepted
        capture.check_stack(stub_api, "2026-10-02")
    assert f"Bearer {SENTINEL}" in _Stub.seen_auth  # sent to the local stub, on the gated call
    assert _Stub.seen_auth[0] is None  # and /livez is called with no token at all


# --- the mock outbox ---------------------------------------------------------------------------


def _write_outbox(folder: Path, digests: int = 1, duplicate_row: bool = False) -> None:
    day = folder / "dallas-2026-10-02"
    day.mkdir(parents=True)
    slack = [{"path": "/chat.postMessage", "body": {"blocks": []}}] * digests
    slack += [{"path": "/files.completeUploadExternal", "body": {}}]
    (day / "mock-slack-requests.jsonl").write_text("\n".join(json.dumps(r) for r in slack))

    def row(key: str, rank: int, profit: int) -> dict[str, object]:
        return {
            "body": {
                "properties": {
                    "Candidate key": {"rich_text": [{"text": {"content": key}}]},
                    "Rank": {"number": rank},
                    "Profit": {"number": profit},
                }
            }
        }

    rows = [{"body": None}, row("dallas:2", 2, 5), row("dallas:1", 1, 7)]
    if duplicate_row:
        rows.append(row("dallas:1", 1, 9))  # the same row written again, later
    (day / "mock-notion-requests.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (day / "pro-forma-rank-1-candidate-1.pdf").write_bytes(b"%PDF")


def test_the_outbox_is_read_in_rank_order_with_a_rewritten_row_counted_once(
    capture: ModuleType, tmp_path: Path
) -> None:
    _write_outbox(tmp_path, duplicate_row=True)

    data = capture.outbox(tmp_path, "2026-10-02")

    assert [r["properties"]["Rank"]["number"] for r in data["rows"]] == [1, 2]
    assert data["rows"][0]["properties"]["Profit"]["number"] == 9  # the later write wins


def test_an_outbox_with_two_digests_or_missing_files_is_refused_with_a_message(
    capture: ModuleType, tmp_path: Path
) -> None:
    _write_outbox(tmp_path / "two", digests=2)
    with pytest.raises(capture.CaptureError, match="2 digests"):
        capture.outbox(tmp_path / "two", "2026-10-02")

    (tmp_path / "empty" / "dallas-2026-10-02").mkdir(parents=True)
    with pytest.raises(capture.CaptureError, match="cannot read"):
        capture.outbox(tmp_path / "empty", "2026-10-02")
    with pytest.raises(capture.CaptureError, match="no mock outbox"):
        capture.outbox(tmp_path / "none", "2026-10-02")


# --- the social card ---------------------------------------------------------------------------


def _row(margin: float) -> list[dict[str, object]]:
    return [
        {
            "properties": {
                "Margin": {"number": margin},
                "Name": {"title": [{"text": {"content": "8773 ORRINMOOR TRL, 75209"}}]},
                "ARV": {"number": 1678067.65},
                "Profit": {"number": 329563.07},
                "Max offer": {"number": 448602.06},
            }
        }
    ]


def test_the_social_card_refuses_a_rank_one_row_below_the_target_it_claims_to_clear(
    capture: ModuleType,
) -> None:
    assert capture.target_margin() == 0.15  # the Dallas pack's target

    row = capture.social_row_from(_row(0.1964))
    assert row["address"] == "8773 Orrinmoor Trl"
    assert row["margin"] == "19.64%"
    with pytest.raises(capture.CaptureError, match="below the target"):
        capture.social_row_from(_row(0.1))
