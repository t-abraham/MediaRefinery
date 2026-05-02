"""MediaRefinery v2 service layer (FastAPI).

Skeleton package. See [docs/adr/ADR-0010-v2-service-architecture.md] and
Phase B of the v2 roadmap. This module is import-safe without the
``[service]`` extra; concrete behavior is added in subsequent PRs.
"""

__all__ = ["app", "routers", "deps", "security", "scheduler", "audit", "models", "web"]
