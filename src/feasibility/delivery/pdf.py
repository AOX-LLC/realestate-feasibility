"""The PDF of a pro-forma: the HTML of `html.py` rendered by WeasyPrint.

WeasyPrint is imported only here and only when a PDF is made (the CLI and the worker), so the API
process never loads it. It is given a fetcher that serves files from this package's `templates/`
and `fonts/` folders and nothing else: markup that got into the page cannot make it fetch a URL
or read a file.
"""

import mimetypes
from pathlib import Path
from typing import Any
from urllib.request import url2pathname

from feasibility.delivery.document import ProformaDocument
from feasibility.delivery.errors import PdfRenderError
from feasibility.delivery.html import FONTS, TEMPLATES, render_html

ALLOWED_FOLDERS = (TEMPLATES.resolve(), FONTS.resolve())
CONTENT_TYPES = {".css": "text/css", ".ttf": "font/ttf", ".otf": "font/otf"}


def allowed_path(url: str) -> Path:
    """The file a `file:` URL names, if it is inside the templates or fonts folder.

    Raises ValueError for any other scheme (http, https, data, ftp), for a query or fragment
    that names something else, and for a path that leaves the two folders by `..` or a link."""
    if not url.startswith("file:"):
        raise ValueError("only files of the pro-forma package may be loaded")
    path = Path(url2pathname(url.split("?")[0].split("#")[0].removeprefix("file:"))).resolve()
    if not any(path.is_relative_to(folder) for folder in ALLOWED_FOLDERS) or not path.is_file():
        raise ValueError("that file is not part of the pro-forma package")
    return path


def safe_fetcher() -> Any:
    from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

    class SafeFetcher(URLFetcher):  # type: ignore[misc]
        def fetch(self, url: str, headers: Any = None) -> Any:
            try:
                path = allowed_path(url)
            except ValueError as error:
                # Fatal: a request for anything else means markup got into the page, and a
                # document with a hole in it is not worth delivering.
                raise FatalURLFetchingError(str(error)) from None
            content_type = CONTENT_TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0]
            return URLFetcherResponse(
                url, body=path.read_bytes(), headers={"Content-Type": content_type or "text/plain"}
            )

    return SafeFetcher(allowed_protocols=("file",))


def render_pdf(document: ProformaDocument) -> bytes:
    from weasyprint import HTML
    from weasyprint.urls import FatalURLFetchingError

    html = HTML(
        string=render_html(document),
        base_url=TEMPLATES.resolve().as_uri() + "/",
        url_fetcher=safe_fetcher(),
    )
    try:
        pdf: bytes = html.write_pdf()
    except FatalURLFetchingError:
        # It derives from BaseException, which a worker's `except Exception` does not catch: left
        # alone it would end the process and the job would be claimed and run again, for ever.
        raise PdfRenderError("blocked_fetch") from None
    return pdf
