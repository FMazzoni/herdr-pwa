"""The one Jinja environment, autoescaping everything, and the responses built from it."""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from fastapi.responses import HTMLResponse
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup

from herdr_pwa.web.assets import SCRIPTS, Assets

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"
FRAGMENT_HEADER: Final = "HP-Fragment"


class Pages:
    """Renders the templates it is handed, by name, into HTML responses; every one autoescapes."""

    def __init__(
        self,
        assets: Assets,
        templates: Mapping[str, str],
        *,
        device_report: bool = False,
        fallback: int = 20,
    ) -> None:
        # autoescape=True rather than select_autoescape, so no file extension can opt out.
        # Held in memory, like the static files: a merge onto disk must not reach old Python.
        self.env = Environment(
            loader=DictLoader(dict(templates)),
            auto_reload=False,
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.shared: dict[str, object] = {
            "asset": assets.url,
            "build": assets.build,
            "scripts": SCRIPTS,
            "device_report": device_report,
            "fallback": fallback,
        }

    def render(self, name: str, context: Mapping[str, object]) -> str:
        """Render one template to a string."""
        return self.env.get_template(name).render({**self.shared, **context})

    def piece(self, name: str, context: Mapping[str, object]) -> Markup:
        """A fragment drawn into a page, exactly as its own route would send it."""
        # Safe as markup: it is this environment's own output, escaped as it was rendered.
        return Markup(self.render(name, context))  # noqa: S704

    def page(self, name: str, context: Mapping[str, object], *, status: int = 200) -> HTMLResponse:
        """A whole document."""
        return HTMLResponse(self.render(name, context), status_code=status)

    def fragment(
        self,
        name: str,
        context: Mapping[str, object],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        """A piece htmx swaps in; marked so the client swaps it even on a failure status."""
        return HTMLResponse(
            self.render(name, context),
            status_code=status,
            headers={**(headers or {}), FRAGMENT_HEADER: "1"},
        )


def read_templates(directory: Path) -> dict[str, str]:
    """Every template under ``directory`` by its name, read once at startup."""
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }
