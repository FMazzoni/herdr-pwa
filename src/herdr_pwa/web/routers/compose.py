"""Writes to an agent: prompts, keys, answers. Empty until the write services land."""

from fastapi import APIRouter, Depends

from herdr_pwa.web.deps import WebState, viewer_guard


def build(web: WebState) -> APIRouter:
    """The compose routes; every one is a write, so the guard origin-checks it."""
    return APIRouter(dependencies=[Depends(viewer_guard(web))])
