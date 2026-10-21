"""The debug-only sink the device-report script posts to; mounted only when the flag is on."""

from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from herdr_pwa.devreport import Batch, ReportStore
from herdr_pwa.validation import summary
from herdr_pwa.web.deps import WebState, viewer_guard

# A keepalive fetch may carry at most 64 KiB, so a bigger body is not from the script.
MAX_BODY_BYTES: Final = 64 * 1024
JSON_TYPE: Final = "application/json"


class _RefusalError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status: int = status


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _RefusalError(413, f"body is over {MAX_BODY_BYTES} bytes")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        # Checked while reading, so a lying Content-Length cannot make the server buffer it all.
        if len(raw) > MAX_BODY_BYTES:
            raise _RefusalError(413, f"body is over {MAX_BODY_BYTES} bytes")
    return bytes(raw)


async def _batch(request: Request) -> Batch:
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != JSON_TYPE:
        raise _RefusalError(415, f"expected {JSON_TYPE}")
    raw = await _bounded_body(request)
    return Batch.model_validate_json(raw.decode("utf-8", errors="replace"))


def build(web: WebState) -> APIRouter:
    """The report sink, behind the same identity and origin gate as every other write."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])
    store = ReportStore(web.settings.state_dir)

    @router.post("/device-report", include_in_schema=False)
    async def device_report(request: Request) -> Response:
        try:
            store.append(await _batch(request))
        except _RefusalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except ValidationError as exc:
            return JSONResponse({"error": f"not a report: {summary(exc)}"}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": f"cannot record: {exc}"}, status_code=500)
        return Response(status_code=204)

    return router
