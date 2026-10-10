# Media on the README front page

Small, optimised copies of what `scripts/capture/capture_proof.py` captured from the seeded
Compose stack (two morning runs on the synthetic snapshot, model calls replayed, delivery in mock
mode). The full-size originals are written outside the repository.

| File | What it is | Real or mock |
| --- | --- | --- |
| `morning-brief.gif` | The digest arrives in a channel and a pro-forma PDF opens from its thread; 27 frames | The channel is a **mock rendering**; the PDF pages are the real PDF |
| `pro-forma-page-1.png` | Page 1 of the rank-1 pro-forma PDF of 2026-10-02, rasterised | A real page |
| `slack-digest-mock.png` | The Slack digest payload the app builds, drawn in this project's style | **Mock rendering**, labelled on the image |
| `notion-table-mock.png` | The Notion rows the app writes, drawn the same way | **Mock rendering**, labelled on the image |

A mock rendering is drawn from the payload files the app writes to its mock outbox. It is not a
screenshot of Slack or Notion and does not copy either product's look; each image says so in a
banner. Everything shown is synthetic (invented street names and figures), and each image was
checked for hostnames, addresses of machines, terminal prompts, internal tool names, keys and
names of people or workspaces.

## Slots waiting for real screenshots

Live Slack and Notion test workspaces are not set up, so these files do not exist yet. When they
do, capture them in a fresh browser profile, hide the sidebar, workspace name, avatars and
logos, apply the same checks, optimise them, and add them here under exactly these names:

| Slot (file name) | What goes in it |
| --- | --- |
| `slack-digest-live.png` | The morning digest in a Slack test channel, with the PDFs in its thread |
| `notion-table-live.png` | The Notion test database after a morning run, one row per candidate |
| `morning-brief-live.gif` | Optional: the digest arriving in the real channel and a PDF opening, as `morning-brief.gif` does for the mock |

`capture_proof.py live-slack` and `live-notion` exist as placeholders: they exit with a message
and capture nothing, because they need a real workspace, a saved browser login in `local/playwright/`
and `DELIVERY_MODE=live`, and none of that has been run.

## Regenerating

With the stack up and both mornings triggered (see the quick start), the read token in
`API_READ_TOKEN` and the stack's `MEDIA_OUT_DIR` as `--stack-out`:

```bash
uv run --no-project --with playwright==1.58.0 --with pypdfium2==5.14.0 --with pillow==12.3.0 \
    python scripts/capture/capture_proof.py all --stack-out "$MEDIA_OUT_DIR"
uv run --no-project --with playwright==1.58.0 --with pypdfium2==5.14.0 --with pillow==12.3.0 \
    python scripts/capture/capture_proof.py optimise
```

The first command writes the full-size files and a 1280x640 social preview to `$MEDIA_OUT/05-proof-kit/`;
the second writes the optimised copies here. The script refuses to write anywhere else (the media
folder, `docs/media/` and `local/` only). The tools are pinned in the command and are not part of
`uv.lock`; a Chromium matching the pinned Playwright must be installed (`playwright install chromium`).
