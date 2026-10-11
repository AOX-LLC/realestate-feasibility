"""Capture the proof-kit media from the seeded Compose stack with Playwright.

Not part of the application and not in uv.lock. Run it with its own pinned tools:

    uv run --no-project --with playwright==1.58.0 --with pypdfium2==5.14.0 --with pillow==12.3.0 \\
        python scripts/capture/capture_proof.py all --stack-out <the stack's MEDIA_OUT_DIR>

It needs the stack running with the two morning runs done (see the README quick start), the read
token in `API_READ_TOKEN` (read from the environment, never printed), and a Chromium that matches
the pinned Playwright (`playwright install chromium`, once). Every browser context is a fresh,
empty profile.

What it draws is labelled on the image. Live Notion and Slack are not set up, so the Slack
digest and the Notion rows are *mock renderings* of the payloads the app builds in mock delivery
mode, in this project's own style (never a copy of either product's look), and the PDF page is
the real PDF rasterised.

Output goes only to `--out` (default `$MEDIA_OUT/proof-kit`, else `local/proof-kit`), and to
`docs/media/` only as the destination of `optimise`.
"""

import argparse
import html
import json
import os
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FONTS = REPO / "src" / "feasibility" / "delivery" / "fonts"
DOCS_MEDIA = REPO / "docs" / "media"
DEFAULT_DAY = "2026-10-02"
MARKET = "dallas"
VIEWPORT = {"width": 1000, "height": 640}  # the GIF
SLACK_SHOT = {"width": 1000, "height": 700}
NOTION_SHOT = {"width": 1500, "height": 540}
LOCAL_API_PREFIXES = ("http://127.0.0.1:", "http://localhost:")
GIF_WIDTH = 760
GIF_COLOURS = 64
LIVE_SLOTS = {
    "live-slack": "slack-digest-live.png",
    "live-notion": "notion-table-live.png",
}


class CaptureError(RuntimeError):
    pass


def media_out_root() -> Path | None:
    """The folder outside the repository for final media. Only `MEDIA_OUT`: the stack's own
    outbox (`MEDIA_OUT_DIR`) is a different folder and must not receive captures."""
    value = os.environ.get("MEDIA_OUT")
    return Path(value).resolve() if value else None


def safe_out(path: Path, *, allow_docs_media: bool = False) -> Path:
    """Refuse to write anywhere but the media folder or local/ (docs/media only for `optimise`)."""
    resolved = path.resolve()
    roots = [REPO / "local"]
    if allow_docs_media:
        roots.append(DOCS_MEDIA)
    root = media_out_root()
    if root is not None:
        roots.append(root)
    if not any(resolved == r.resolve() or r.resolve() in resolved.parents for r in roots):
        raise CaptureError(f"refusing to write outside the media folder or local/: {resolved}")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CaptureError(f"cannot create {resolved}: {error.strerror}") from None
    return resolved


# --- the stack ----------------------------------------------------------------------------------


def check_api_url(api: str) -> None:
    """The read token goes only to the stack on this machine."""
    if not api.startswith(LOCAL_API_PREFIXES):
        raise CaptureError("--api must be http://127.0.0.1:<port> or http://localhost:<port>")


def api_get(api: str, path: str, token: str | None) -> Any:
    request = urllib.request.Request(api + path)  # noqa: S310 - checked to be a local http URL
    if token:
        # Unredirected: a redirect to another host must not carry the token along.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise CaptureError(f"GET {path} answered {error.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise CaptureError(f"GET {path}: no usable answer from the API at {api}") from None


def check_stack(api: str, day: str) -> int:
    """The stack must be up and have delivered the day; returns the run id."""
    token = os.environ.get("API_READ_TOKEN")
    if not token:
        raise CaptureError("API_READ_TOKEN is not set (the read token of the running stack)")
    check_api_url(api)
    api_get(api, "/livez", None)
    runs = api_get(api, "/sourcing/runs", token)
    items = runs["items"] if isinstance(runs, dict) else runs
    matching = [r for r in items if str(r.get("as_of")) == day]
    if not matching:
        raise CaptureError(f"the stack has no run for {day}: trigger the morning run first")
    run_id = int(matching[0]["id"])
    deliveries = api_get(api, f"/sourcing/runs/{run_id}/deliveries", token)
    statuses = {d["status"] for d in deliveries["items"]}
    if statuses != {"sent"}:
        raise CaptureError(f"run {run_id}: delivery rows are {sorted(statuses)}, expected all sent")
    return run_id


def read_lines(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        raise CaptureError(f"cannot read {path.name} as lines of JSON in {path.parent}") from None


def outbox(stack_out: Path, day: str) -> dict[str, Any]:
    folder = stack_out / f"{MARKET}-{day}"
    if not folder.is_dir():
        raise CaptureError(f"no mock outbox at {folder}")
    digests = [
        r["body"]
        for r in read_lines(folder / "mock-slack-requests.jsonl")
        if r.get("path") == "/chat.postMessage"
    ]
    if len(digests) != 1:
        raise CaptureError(
            f"the outbox holds {len(digests)} digests, expected 1: use a fresh MEDIA_OUT_DIR"
        )
    by_key: dict[str, dict[str, Any]] = {}
    for request in read_lines(folder / "mock-notion-requests.jsonl"):
        body = request.get("body")
        if body and "properties" in body:
            key = body["properties"]["Candidate key"]["rich_text"][0]["text"]["content"]
            by_key[key] = body  # a later write to the same row replaces the earlier one
    rows = sorted(by_key.values(), key=lambda b: b["properties"]["Rank"]["number"])
    pdfs = sorted(folder.glob("pro-forma-rank-*.pdf"), key=lambda p: int(p.name.split("-")[3]))
    if not pdfs or not rows:
        raise CaptureError(f"{folder} holds no PDFs or no Notion rows")
    return {"digest": digests[0], "rows": rows, "pdfs": pdfs}


# --- the PDF ------------------------------------------------------------------------------------


def rasterise(pdf: Path, width: int = 1600) -> list[Any]:
    import pypdfium2 as pdfium  # imported here: only the PDF commands need it

    document = pdfium.PdfDocument(str(pdf))
    pages = []
    for index in range(len(document)):
        page = document[index]
        scale = width / page.get_size()[0]
        pages.append(page.render(scale=scale).to_pil().convert("RGB"))
    return pages


def data_uri(image: Any, width: int) -> str:
    import base64
    import io

    small = image.resize((width, round(image.height * width / image.width)))
    buffer = io.BytesIO()
    small.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def cmd_pdf(out: Path, data: dict[str, Any]) -> list[Path]:
    pages = rasterise(data["pdfs"][0])
    written = []
    for number, image in enumerate(pages, start=1):
        target = out / f"pro-forma-page-{number}.png"
        image.save(target, optimize=True)
        written.append(target)
    return written


# --- the browser --------------------------------------------------------------------------------


def stage_file(out: Path, name: str) -> Path:
    """The page template with the font folder filled in, written beside the output."""
    work = out / ".work"
    work.mkdir(exist_ok=True)
    html = (HERE / name).read_text(encoding="utf-8").replace("{{FONTS}}", FONTS.as_uri())
    target = work / name
    target.write_text(html, encoding="utf-8")
    return target


def launch(playwright: Any) -> Any:
    return playwright.chromium.launch(headless=True)


def open_stage(
    browser: Any, out: Path, viewport: dict[str, int], **context_args: Any
) -> tuple[Any, Any]:
    context = browser.new_context(viewport=viewport, device_scale_factor=1, **context_args)
    page = context.new_page()
    page.goto(stage_file(out, "stage.html").as_uri())
    page.evaluate("document.fonts.ready")
    return context, page


def js(value: Any) -> str:
    return json.dumps(value)


def cmd_mock(out: Path, data: dict[str, Any], day: str) -> list[Path]:
    from playwright.sync_api import sync_playwright

    names = [p.name for p in data["pdfs"]]
    written = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        context, page = open_stage(browser, out, SLACK_SHOT)
        page.evaluate(f"slackMock({js(data['digest'])}, {js(names)})")
        page.evaluate("step.arrive(); " + "".join(f"step.file({i});" for i in range(len(names))))
        page.wait_for_timeout(900)
        target = out / "slack-digest-mock.png"
        page.screenshot(path=str(target), full_page=True)
        written.append(target)
        context.close()

        context, page = open_stage(browser, out, NOTION_SHOT)
        page.evaluate(f"notionMock({js(data['rows'])}, {js(day)})")
        page.wait_for_timeout(300)
        target = out / "notion-table-mock.png"
        page.screenshot(path=str(target), full_page=True)
        written.append(target)
        context.close()
        browser.close()
    return written


def ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def cmd_gif(out: Path, data: dict[str, Any], day: str) -> Path:
    """Play the morning in the stage page, one screenshot per step, and join them into a GIF.

    Screenshots with transitions switched off, not a screen recording: every frame is exact, so
    the GIF is small and has no compression noise.
    """
    import io

    from PIL import Image
    from playwright.sync_api import sync_playwright

    names = [p.name for p in data["pdfs"]]
    pages = rasterise(data["pdfs"][0])
    sources = [data_uri(image, 900) for image in pages]
    frames: list[tuple[Image.Image, int]] = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        context, page = open_stage(browser, out, VIEWPORT)
        page.add_style_tag(content="*, *::before, *::after { transition: none !important; }")
        page.evaluate(f"slackMock({js(data['digest'])}, {js(names)})")
        page.evaluate("step.fixHeight()")

        def snap(milliseconds: int) -> None:
            image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
            frames.append((image, milliseconds))

        snap(900)  # an empty channel, before the run
        for level in (0.3, 0.65, 1.0):
            page.evaluate(f"step.fade({level})")
            snap(130 if level < 1 else 1300)  # the digest arrives
        top = page.evaluate("step.maxScroll()")
        for index in range(1, 9):  # down to the PDFs in the thread
            page.evaluate(f"step.scrollTo({top * ease(index / 8):.0f})")
            snap(110)
        snap(300)
        for index in range(len(names)):
            page.evaluate(f"step.file({index})")
            snap(220)
        snap(500)
        page.evaluate("step.hot(0, true)")
        snap(600)
        page.evaluate(f"step.open({js(sources[0])}, {js(names[0])}, 1, {len(pages)})")
        page.wait_for_timeout(150)
        snap(2200)
        for index in range(1, 7):  # scroll the first page
            page.evaluate(f"step.viewerScroll({260 * ease(index / 6):.0f})")
            snap(120)
        snap(900)
        if len(sources) > 1:
            page.evaluate(f"step.page({js(sources[1])}, 2, {len(pages)})")
            page.wait_for_timeout(150)
            snap(3000)
        context.close()
        browser.close()
    return save_gif(frames, out / "morning-brief.gif")


def save_gif(frames: list[tuple[Any, int]], target: Path) -> Path:
    from PIL import Image

    width = GIF_WIDTH
    small = [
        image.resize((width, round(image.height * width / image.width)), Image.Resampling.LANCZOS)
        for image, _ in frames
    ]
    # One palette for every frame (from a montage of them), so colours do not flicker.
    sheet = Image.new("RGB", (width, small[0].height * len(small)))
    for index, image in enumerate(small):
        sheet.paste(image, (0, index * small[0].height))
    palette = sheet.quantize(
        colors=GIF_COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    paletted = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in small]
    paletted[0].save(
        target,
        save_all=True,
        append_images=paletted[1:],
        duration=[milliseconds for _, milliseconds in frames],
        loop=0,
        optimize=True,
    )
    return target


def cmd_social(out: Path, row: dict[str, str]) -> Path:
    from playwright.sync_api import sync_playwright

    narrative = json.loads((REPO / "evals" / "scorecards" / "narrative.json").read_text())[
        "summary"
    ]
    page_html = (HERE / "social.html").read_text(encoding="utf-8")
    page_html = page_html.replace("{{FONTS}}", FONTS.as_uri())
    for key, value in {
        "ADDRESS": row["address"],
        "ARV": row["arv"],
        "PROFIT": row["profit"],
        "MARGIN": row["margin"],
        "MAXOFFER": row["max_offer"],
        "VERDICT": row["verdict"],
        "ACCEPTED": f"{narrative['accepted']} / {narrative['cases_scored']}",
        "FIGURES": f"{narrative['figure_exact_of_accepted'] * 100:.0f}%",
    }.items():
        page_html = page_html.replace("{{" + key + "}}", html.escape(value))
    work = out / ".work"
    work.mkdir(exist_ok=True)
    page_file = work / "social.html"
    page_file.write_text(page_html, encoding="utf-8")
    target = out / "social-preview.png"
    with sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(
            viewport={"width": 1280, "height": 640}, device_scale_factor=1
        )
        page = context.new_page()
        page.goto(page_file.as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.screenshot(path=str(target), clip={"x": 0, "y": 0, "width": 1280, "height": 640})
        context.close()
        browser.close()
    return target


def target_margin() -> float:
    """The Dallas pack's target margin, as a ratio (the PDF's verdict compares against it)."""
    pack = tomllib.loads(
        (REPO / "src" / "feasibility" / "markets" / "packs" / "dallas.toml").read_text("utf-8")
    )
    return float(pack["cost_assumptions"]["target"]["margin_pct"]) / 100


def social_row_from(rows: list[dict[str, Any]]) -> dict[str, str]:
    """The figures of the rank 1 row, with the verdict worked out from them. Refuses a row that
    is below the target, because the card's claim would then be false."""
    props = rows[0]["properties"]
    margin = props["Margin"]["number"]
    if margin < target_margin():
        raise CaptureError(
            "the rank 1 row is below the target margin: the social card says it clears it"
        )

    def money(name: str) -> str:
        return f"${props[name]['number']:,.2f}"

    street = props["Name"]["title"][0]["text"]["content"].split(",")[0].title()
    return {
        "address": street,
        "arv": money("ARV"),
        "profit": money("Profit"),
        "margin": f"{margin * 100:.2f}%",
        "max_offer": money("Max offer"),
        "verdict": "The margin meets or beats the target margin.",
    }


# --- optimised copies for the README -------------------------------------------------------------

COMMITTED = {
    "morning-brief.gif": ("morning-brief.gif", None),
    "pro-forma-page-1.png": ("pro-forma-page-1.png", 1100),
    "slack-digest-mock.png": ("slack-digest-mock.png", 1000),
    "notion-table-mock.png": ("notion-table-mock.png", 1500),
}
MAX_BYTES = 3_000_000


def cmd_optimise(src: Path, dest: Path) -> list[Path]:
    from PIL import Image

    written = []
    for source_name, (target_name, width) in COMMITTED.items():
        source = src / source_name
        if not source.exists():
            raise CaptureError(f"{source} is missing: capture it first")
        target = dest / target_name
        if width is None:
            shutil.copyfile(source, target)
        else:
            image = Image.open(source).convert("RGB")
            if image.width > width:
                image = image.resize(
                    (width, round(image.height * width / image.width)), Image.Resampling.LANCZOS
                )
            image.quantize(
                colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
            ).save(target, optimize=True)
        if target.stat().st_size > MAX_BYTES:
            raise CaptureError(
                f"{target.name} is {target.stat().st_size:,} bytes, over {MAX_BYTES:,}"
            )
        written.append(target)
    return written


# --- the command line ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "command",
        choices=["check", "pdf", "mock", "gif", "social", "all", "optimise", *LIVE_SLOTS],
    )
    parser.add_argument("--stack-out", type=Path, help="the stack's MEDIA_OUT_DIR (mock outbox)")
    parser.add_argument("--day", default=DEFAULT_DAY)
    parser.add_argument("--api", default="http://127.0.0.1:4501")
    parser.add_argument(
        "--out",
        type=Path,
        help="output folder (default $MEDIA_OUT/proof-kit, else local/proof-kit)",
    )
    args = parser.parse_args()

    try:
        if args.command in LIVE_SLOTS:
            raise CaptureError(
                f"{args.command} is not built: it needs a real workspace, a saved browser login "
                f"in local/playwright/ and DELIVERY_MODE=live, none of which exist yet. The slot "
                f"is docs/media/{LIVE_SLOTS[args.command]}."
            )
        root = media_out_root()
        out = safe_out(args.out or ((root / "proof-kit") if root else REPO / "local" / "proof-kit"))
        if args.command == "optimise":
            for path in cmd_optimise(out, safe_out(DOCS_MEDIA, allow_docs_media=True)):
                print(f"{path.relative_to(REPO)} {path.stat().st_size:,} bytes")
            return 0
        run_id = check_stack(args.api, args.day)
        print(f"stack ok: run {run_id} for {args.day}, every delivery row sent", file=sys.stderr)
        if args.command == "check":
            return 0
        if args.stack_out is None:
            raise CaptureError("--stack-out is required")
        data = outbox(args.stack_out, args.day)
        row = social_row_from(data["rows"]) if args.command in ("social", "all") else {}
        results: list[Path] = []
        if args.command in ("pdf", "all"):
            results += cmd_pdf(out, data)
        if args.command in ("mock", "all"):
            results += cmd_mock(out, data, args.day)
        if args.command in ("gif", "all"):
            results.append(cmd_gif(out, data, args.day))
        if args.command in ("social", "all"):
            results.append(cmd_social(out, row))
        for path in results:
            print(f"{path} {path.stat().st_size:,} bytes")
    except CaptureError as error:
        print(f"capture_proof: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
