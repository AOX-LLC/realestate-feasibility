"""The HTML of a pro-forma: a Jinja2 template over a `ProformaDocument`.

Autoescape is on and an undefined name is an error, so a field that is renamed breaks the render
instead of printing nothing. The template only places strings; it never formats a number. Nothing
in it is evaluated from data: data is only ever a value.
"""

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from feasibility.delivery.document import ProformaDocument

TEMPLATES = Path(__file__).resolve().parent / "templates"
FONTS = Path(__file__).resolve().parent / "fonts"


@lru_cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"], default=True, default_for_string=True),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_html(document: ProformaDocument) -> str:
    """The page as a string. Its stylesheets are relative links into `templates/`."""
    return _environment().get_template("proforma.html.j2").render(d=document)
