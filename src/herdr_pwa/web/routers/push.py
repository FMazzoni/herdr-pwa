"""Pairing and Web Push subscription routes. Empty until they are wired to the push library."""

from fastapi import APIRouter, Depends

from herdr_pwa.web.deps import WebState, viewer_guard


def build(web: WebState) -> APIRouter:
    """The push routes, behind the same identity gate as every other router."""
    return APIRouter(prefix="/push", dependencies=[Depends(viewer_guard(web))])
