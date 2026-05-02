"""Dashboard static-asset serving and security-headers middleware (Phase E PR 1).

The frontend Vite project at ``frontend/`` builds into
``src/mediarefinery/web/`` so the wheel ships the dashboard as static
assets. This module mounts that directory on the FastAPI app and
attaches the security headers required by the threat model:

- T01 (XSS / cookie theft): a strict Content-Security-Policy with no
  inline script and no third-party origins.
- T11 (no third-party CDNs / fonts / analytics): ``connect-src``,
  ``script-src``, ``style-src``, ``font-src``, ``img-src`` are all
  ``'self'`` with the minimum extras required by Vite output
  (``data:`` for tiny inlined images).
- T18 (CSRF): ``form-action 'self'`` and ``frame-ancestors 'none'``.

Intentionally permissive bits, with reasoning:

- ``style-src 'self' 'unsafe-inline'``: Tailwind generates a single
  bundled stylesheet at build time, but Headless UI and React's
  ``style={{...}}`` props emit inline ``style`` attributes. CSP3
  ``'unsafe-inline'`` *for styles only* is the standard accommodation
  and does not enable script execution. Re-evaluate in the polish PR
  with hashes if we want to tighten further.
- ``img-src 'self' data: blob:``: ``blob:`` is needed for any future
  preview-this-asset flow that pipes Immich-fetched bytes into an
  ``<img>`` without round-tripping through our backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

CSP_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
)


def _build_middleware_class():
    from starlette.middleware.base import BaseHTTPMiddleware

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        """Attach CSP and adjacent security headers to every response."""

        def __init__(self, app, *, hsts: bool) -> None:
            super().__init__(app)
            self._hsts = hsts

        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), interest-cohort=()",
            )
            if self._hsts:
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )
            return response

    return SecurityHeadersMiddleware


def default_web_root() -> Path:
    """Resolve the dashboard bundle directory.

    ``MR_WEB_ROOT`` overrides the bundled location — useful for tests
    that need to inject a stub bundle and for operators who pre-build
    the frontend into an alternate path.
    """

    import os

    override = os.environ.get("MR_WEB_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web"


def mount_web(app: "FastAPI", *, web_root: Path, hsts: bool) -> None:
    """Attach the security-headers middleware and, if a built bundle
    exists, mount it at ``/`` so the dashboard loads from the same
    origin as the API.

    The middleware is registered unconditionally — even if the bundle
    has not been built (e.g. running pure backend tests) the API
    responses still carry CSP. The static mount only happens when
    ``index.html`` is present, so a missing bundle is not a 500 at
    boot.
    """

    from starlette.staticfiles import StaticFiles

    app.add_middleware(_build_middleware_class(), hsts=hsts)
    index = web_root / "index.html"
    if index.is_file():
        app.mount(
            "/",
            StaticFiles(directory=str(web_root), html=True),
            name="web",
        )


__all__ = ["CSP_POLICY", "default_web_root", "mount_web"]
