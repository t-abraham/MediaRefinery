"""Service-level configuration resolved from environment variables.

Per-user configuration (categories, policies) lives in the v2 SQLite
database; this module covers the operator-controlled deployment knobs
that bind ports, configure trusted proxies, and pin the upstream
Immich URL.

Demo mode (``MR_DEMO=1``, threat-model T16) is a startup-time flag for
hosted demos and CI. When enabled it is mutually exclusive with
``MR_MASTER_KEY``: prod and demo deployments must never share an
environment, so :func:`load_service_config` raises ``RuntimeError`` if
both are set. The synthetic-data fixtures the demo banner advertises
are scoped to a later Phase E PR; this module only owns the gate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8080
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60  # 12h sliding
DEFAULT_REVALIDATE_INTERVAL_SECONDS = 5 * 60  # 5min cap on Immich /users/me hits
DEFAULT_LOGIN_RATE_PER_MIN = 5
DEFAULT_DATA_DIR = Path("/data")


@dataclass(frozen=True)
class ServiceConfig:
    immich_base_url: str
    base_url: str
    data_dir: Path
    trusted_proxies: tuple[str, ...]
    session_ttl_seconds: int
    revalidate_interval_seconds: int
    login_rate_per_min: int
    cookie_secure: bool
    demo_mode: bool = False

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state-v2.db"

    @property
    def master_key_path(self) -> Path:
        return self.data_dir / "master.key"


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def load_service_config(env: dict[str, str] | None = None) -> ServiceConfig:
    env = os.environ if env is None else env
    immich = env.get("MR_IMMICH_BASE_URL", "").rstrip("/")
    if not immich:
        raise RuntimeError(
            "MR_IMMICH_BASE_URL must be set to the upstream Immich base URL"
        )
    base_url = env.get("MR_BASE_URL", "http://localhost:8080").rstrip("/")
    data_dir = Path(env.get("MR_DATA_DIR", str(DEFAULT_DATA_DIR)))

    demo_mode = env.get("MR_DEMO", "") == "1"
    if demo_mode and env.get("MR_MASTER_KEY"):
        raise RuntimeError(
            "MR_DEMO=1 and MR_MASTER_KEY are mutually exclusive: demo mode "
            "operates on synthetic data only and must never share an "
            "environment with a production master key (threat-model T16)."
        )

    return ServiceConfig(
        immich_base_url=immich,
        base_url=base_url,
        data_dir=data_dir,
        trusted_proxies=_csv(env.get("MR_TRUSTED_PROXIES")),
        session_ttl_seconds=int(env.get("MR_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)),
        revalidate_interval_seconds=int(
            env.get("MR_REVALIDATE_INTERVAL_SECONDS", DEFAULT_REVALIDATE_INTERVAL_SECONDS)
        ),
        login_rate_per_min=int(env.get("MR_LOGIN_RATE_PER_MIN", DEFAULT_LOGIN_RATE_PER_MIN)),
        cookie_secure=base_url.startswith("https://"),
        demo_mode=demo_mode,
    )


__all__ = [
    "DEFAULT_LOGIN_RATE_PER_MIN",
    "DEFAULT_REVALIDATE_INTERVAL_SECONDS",
    "DEFAULT_SESSION_TTL_SECONDS",
    "ServiceConfig",
    "load_service_config",
]
