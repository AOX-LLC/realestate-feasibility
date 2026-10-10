"""Where a mock delivery leaves what it would have sent, when a folder is configured (MEDIA_OUT).

Mock delivery sends nothing, so without this a person cannot see what a morning run produced. The
folder gets one line of JSON per request a mock transport answered (the payloads, which hold no
token because a mock transport has none) and each file the mock Slack was asked to take, under the
name the client chose, which is built from a rank and an id and never from an address.
"""

import json
import re
from pathlib import Path
from typing import Any

FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")


class Outbox:
    def __init__(self, folder: Path) -> None:
        self._folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def record(self, target: str, method: str, path: str, body: dict[str, Any] | None) -> None:
        line = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True)
        with (self._folder / f"mock-{target}-requests.jsonl").open("a", encoding="utf-8") as out:
            out.write(line + "\n")

    def save_file(self, name: str, content: bytes) -> Path | None:
        """Keep a file the mock Slack was sent; a name that is not a plain file name is not kept."""
        if not FILE_NAME.fullmatch(name) or name.startswith("."):
            return None
        path = self._folder / name
        path.write_bytes(content)
        return path
