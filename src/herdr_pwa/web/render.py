"""The one Jinja environment, autoescaping everything, and the responses built from it."""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from herdr_pwa.web.assets import Assets

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"
FRAGMENT_HEADER: Final = "HP-Fragment"


class Pages:
    """Renders templates into HTML responses; every template autoescapes."""

    def __init__(
        self,
        assets: Assets,
        directory: Path = TEMPLATES_DIR,
        *,
        device_report: bool = False,
        fallback: int = 20,
    ) -> None:
        # autoescape=True rather than select_autoescape, so no file extension can opt out.
        self.env = Environment(
            loader=FileSystemLoader(directory),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.shared: dict[str, object] = {
            "asset": assets.url,
            "build": assets.build,
            "device_report": device_report,
            "fallback": fallback,
        }

    def render(self, name: str, context: Mapping[str, object]) -> str:
        """Render one template to a string."""
        return self.env.get_template(name).render({**self.shared, **context})

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
