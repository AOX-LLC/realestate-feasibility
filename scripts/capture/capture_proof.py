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

Output goes only to `--out` (default `$MEDIA_OUT/05-proof-kit`), `docs/media/` and `local/`.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FONTS = REPO / "src" / "feasibility" / "delivery" / "fonts"
DOCS_MEDIA = REPO / "docs" / "media"
ALLOWED_OUT_ROOTS = [DOCS_MEDIA, REPO / "local"]
DEFAULT_DAY = "2026-10-02"
MARKET = "dallas"
VIEWPORT = {"width": 1000, "height": 640}
GIF_WIDTH = 760
GIF_COLOURS = 64
LIVE_SLOTS = {
    "live-slack": "slack-digest-live.png",
    "live-notion": "notion-table-live.png",
}


class CaptureError(RuntimeError):
    pass


def media_out_root() -> Path | None:
    value = os.environ.get("MEDIA_OUT") or os.environ.get("MEDIA_OUT_DIR")
    return Path(value).resolve() if value else None


def safe_out(path: Path) -> Path:
    """Refuse to write anywhere but the media folder, docs/media or local/."""
    resolved = path.resolve()
    roots = [*ALLOWED_OUT_ROOTS]
    root = media_out_root()
    if root is not None:
        roots.append(root)
    if not any(resolved == r.resolve() or r.resolve() in resolved.parents for r in roots):
        raise CaptureError(
            f"refusing to write outside the media folder, docs/media or local: {resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# --- the stack ----------------------------------------------------------------------------------


def api_get(api: str, path: str, token: str | None) -> Any:
    request = urllib.request.Request(api + path)  # noqa: S310 - the local API, http on 127.0.0.1
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise CaptureError(f"GET {path} answered {error.code}") from None
    except urllib.error.URLError:
        raise CaptureError(f"GET {path}: the API is not reachable at {api}") from None


def check_stack(api: str, day: str) -> int:
    """The stack must be up and have delivered the day; returns the run id."""
    token = os.environ.get("API_READ_TOKEN")
    if not token:
        raise CaptureError("API_READ_TOKEN is not set (the read token of the running stack)")
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


def outbox(stack_out: Path, day: str) -> dict[str, Any]:
    folder = stack_out / f"{MARKET}-{day}"
    if not folder.is_dir():
        raise CaptureError(f"no mock outbox at {folder}")
    slack = [
        json.loads(line) for line in (folder / "mock-slack-requests.jsonl").read_text().splitlines()
    ]
    digest = next(r["body"] for r in slack if r.get("path") == "/chat.postMessage")
    notion = [
        json.loads(line)
        for line in (folder / "mock-notion-requests.jsonl").read_text().splitlines()
    ]
    rows = [r["body"] for r in notion if r.get("body") and "properties" in r["body"]]
    rows.sort(key=lambda b: b["properties"]["Rank"]["number"])
    pdfs = sorted(folder.glob("pro-forma-rank-*.pdf"), key=lambda p: int(p.name.split("-")[3]))
    if not pdfs or not rows:
        raise CaptureError(f"{folder} holds no PDFs or no Notion rows")
    return {"digest": digest, "rows": rows, "pdfs": pdfs}


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
        context, page = open_stage(browser, out, {"width": 1000, "height": 700})
        page.evaluate(f"slackMock({js(data['digest'])}, {js(names)}, {js(day)})")
        page.evaluate("step.arrive(); " + "".join(f"step.file({i});" for i in range(len(names))))
        page.wait_for_timeout(900)
        target = out / "slack-digest-mock.png"
        page.screenshot(path=str(target), full_page=True)
        written.append(target)
        context.close()

        context, page = open_stage(browser, out, {"width": 1500, "height": 540})
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
        page.evaluate(f"slackMock({js(data['digest'])}, {js(names)}, {js(day)})")
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


def cmd_social(out: Path) -> Path:
    from playwright.sync_api import sync_playwright

    narrative = json.loads((REPO / "evals" / "scorecards" / "narrative.json").read_text())[
        "summary"
    ]
    data = outbox_for_social()
    html = (HERE / "social.html").read_text(encoding="utf-8").replace("{{FONTS}}", FONTS.as_uri())
    for key, value in {
        "ADDRESS": data["address"],
        "ARV": data["arv"],
        "PROFIT": data["profit"],
        "MARGIN": data["margin"],
        "MAXOFFER": data["max_offer"],
        "ACCEPTED": f"{narrative['accepted']} / {narrative['cases_scored']}",
    }.items():
        html = html.replace("{{" + key + "}}", value)
    work = out / ".work"
    work.mkdir(exist_ok=True)
    page_file = work / "social.html"
    page_file.write_text(html, encoding="utf-8")
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


SOCIAL_ROW: dict[str, str] = {}


def outbox_for_social() -> dict[str, str]:
    if not SOCIAL_ROW:
        raise CaptureError("the social preview needs the stack's rank 1 row (run with --stack-out)")
    return SOCIAL_ROW


def social_row_from(rows: list[dict[str, Any]]) -> dict[str, str]:
    props = rows[0]["properties"]

    def money(name: str) -> str:
        value = props[name]["number"]
        return f"${value:,.2f}"

    street = props["Name"]["title"][0]["text"]["content"].split(",")[0].title()
    return {
        "address": street,
        "arv": money("ARV"),
        "profit": money("Profit"),
        "margin": f"{props['Margin']['number'] * 100:.2f}%",
        "max_offer": money("Max offer"),
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
    parser.add_argument("--out", type=Path, help="output folder (default $MEDIA_OUT/05-proof-kit)")
    args = parser.parse_args()

    try:
        if args.command in LIVE_SLOTS:
            raise CaptureError(
                f"{args.command} is not built: it needs a real workspace, a saved browser login "
                f"in local/playwright/ and DELIVERY_MODE=live, none of which exist yet. The slot "
                f"is "
                f"docs/media/{LIVE_SLOTS[args.command]}."
            )
        root = media_out_root()
        out = safe_out(args.out or ((root / "05-proof-kit") if root else REPO / "local" / "media"))
        if args.command == "optimise":
            for path in cmd_optimise(out, safe_out(DOCS_MEDIA)):
                print(f"{path.relative_to(REPO)} {path.stat().st_size:,} bytes")
            return 0
        run_id = check_stack(args.api, args.day)
        print(f"stack ok: run {run_id} for {args.day}, every delivery row sent", file=sys.stderr)
        if args.command == "check":
            return 0
        if args.stack_out is None:
            raise CaptureError("--stack-out is required")
        data = outbox(args.stack_out, args.day)
        SOCIAL_ROW.update(social_row_from(data["rows"]))
        results: list[Path] = []
        if args.command in ("pdf", "all"):
            results += cmd_pdf(out, data)
        if args.command in ("mock", "all"):
            results += cmd_mock(out, data, args.day)
        if args.command in ("gif", "all"):
            results.append(cmd_gif(out, data, args.day))
        if args.command in ("social", "all"):
            results.append(cmd_social(out))
        for path in results:
            print(f"{path} {path.stat().st_size:,} bytes")
    except CaptureError as error:
        print(f"capture_proof: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
