"""FastAPI application factory and uvicorn entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .config import ServiceConfig, load_service_config
from .security import (
    AesGcmCipher,
    InMemoryRateLimiter,
    SessionCookieSigner,
    configure_json_logging,
    derive_cookie_signing_key,
    load_or_create_master_key,
)
from .state_v2 import StateStoreV2

API_V1_PREFIX = "/api/v1"


def create_app(*, config: ServiceConfig | None = None):
    """Build the FastAPI app.

    *config* is injectable for tests; production callers omit it and
    rely on environment variables.
    """

    from fastapi import FastAPI

    if config is None:
        config = load_service_config()

    logger = configure_json_logging()
    if config.demo_mode:
        logger.warning(
            "MR_DEMO=1 active — synthetic data only, do not connect a real Immich",
            extra={"event": "demo_mode.active"},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        master_key = load_or_create_master_key(path=config.master_key_path)
        store = StateStoreV2(config.state_db_path)
        store.initialize()
        cipher = AesGcmCipher(master_key.key)
        signing_key = derive_cookie_signing_key(master_key.key)
        signer = SessionCookieSigner(
            signing_key, max_age_seconds=config.session_ttl_seconds
        )
        if config.demo_mode:
            from .demo_fixtures import (
                build_demo_immich_client,
                build_demo_runner_factories,
                seed_demo_model,
            )

            immich_client = build_demo_immich_client(
                base_url=config.immich_base_url
            )
            seed_demo_model(store._conn)
            app.state.runner_factories = build_demo_runner_factories()
        else:
            immich_client = httpx.Client(
                base_url=config.immich_base_url, timeout=10.0
            )
            app.state.runner_factories = None
        login_limiter = InMemoryRateLimiter(
            max_events=config.login_rate_per_min, window_seconds=60.0
        )
        app.state.config = config
        app.state.store = store
        app.state.cipher = cipher
        app.state.signer = signer
        app.state.immich_client = immich_client
        app.state.login_limiter = login_limiter
        try:
            yield
        finally:
            immich_client.close()
            store.close()

    app = FastAPI(
        title="MediaRefinery",
        version="2.0.0a0",
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    from .routers import (
        build_audit_router,
        build_auth_router,
        build_health_router,
        build_me_config_router,
        build_me_router,
        build_models_router,
        build_scans_router,
        build_setup_router,
    )

    app.include_router(build_setup_router(), prefix=API_V1_PREFIX)
    app.include_router(build_auth_router(), prefix=API_V1_PREFIX)
    app.include_router(build_me_router(), prefix=API_V1_PREFIX)
    app.include_router(build_me_config_router(), prefix=API_V1_PREFIX)
    app.include_router(build_scans_router(), prefix=API_V1_PREFIX)
    app.include_router(build_audit_router(), prefix=API_V1_PREFIX)
    app.include_router(build_models_router(), prefix=API_V1_PREFIX)
    app.include_router(build_health_router(), prefix=API_V1_PREFIX)

    from .web import default_web_root, mount_web

    mount_web(app, web_root=default_web_root(), hsts=config.cookie_secure)
    return app


def run() -> None:
    import uvicorn

    config = load_service_config()
    uvicorn.run(
        "mediarefinery.service.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        forwarded_allow_ips=",".join(config.trusted_proxies) or None,
    )


__all__ = ["API_V1_PREFIX", "create_app", "run"]
