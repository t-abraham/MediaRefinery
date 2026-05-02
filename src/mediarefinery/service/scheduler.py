"""Background scan scheduler.

Phase B PR 5 ships a minimal in-process scheduler with:

- per-user concurrency cap (one active scan; ``submit_scan`` raises
  :class:`ScanRejected` with reason ``concurrency_cap`` on duplicate)
- per-user daily quota (default 24, configurable, raises with reason
  ``daily_quota``)
- a synthetic scan body that records 2 dry-run actions and a summary,
  proving the HTTP plumbing and isolation invariants without touching
  Immich. Phase C/D wires the real pipeline.

APScheduler is brought in as a dep but the synthetic scanner runs
inline on a daemon thread; the APScheduler import is deferred so the
test suite does not need its time-zone DB at import time.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .state_v2 import StateStoreV2

log = logging.getLogger("mediarefinery.service.scheduler")

DEFAULT_DAILY_QUOTA = 24


class ScanRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SubmittedScan:
    run_id: int


def _today_start_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00")


def submit_scan(
    *,
    store: StateStoreV2,
    user_id: str,
    daily_quota: int = DEFAULT_DAILY_QUOTA,
    runner: Callable[[StateStoreV2, str, int], None] | None = None,
) -> SubmittedScan:
    scoped = store.with_user(user_id)
    if scoped.has_active_run():
        raise ScanRejected("concurrency_cap")
    if scoped.runs_started_today(since_iso=_today_start_iso()) >= daily_quota:
        raise ScanRejected("daily_quota")
    run_id = scoped.start_run(dry_run=True, command="scan")
    scoped.write_audit(action="scan.start", run_id=run_id)
    if runner is None:
        runner = synthetic_runner
    thread = threading.Thread(
        target=runner,
        args=(store, user_id, run_id),
        name=f"scan-{user_id}-{run_id}",
        daemon=True,
    )
    thread.start()
    return SubmittedScan(run_id=run_id)


def synthetic_runner(store: StateStoreV2, user_id: str, run_id: int) -> None:
    """Records two dry-run actions and finishes the run.

    Used in Phase B to prove the HTTP plumbing. Phase D replaces this
    with the real pipeline.
    """

    scoped = store.with_user(user_id)
    try:
        for asset_id, action_name in (("synthetic-1", "tag"), ("synthetic-2", "tag")):
            scoped.upsert_asset(asset_id=asset_id, media_type="image")
            scoped.record_action(
                run_id=run_id,
                asset_id=asset_id,
                action_name=action_name,
                dry_run=True,
                would_apply=True,
                success=True,
            )
        scoped.write_audit(action="scan.finish", run_id=run_id)
        scoped.finish_run(
            run_id,
            status="completed",
            summary_json=json.dumps({"processed": 2, "synthetic": True}),
        )
    except Exception:  # pragma: no cover - defensive
        scoped.finish_run(run_id, status="failed")
        log.exception("synthetic scan failed", extra={"user_id": user_id, "run_id": run_id})


__all__ = [
    "DEFAULT_DAILY_QUOTA",
    "ScanRejected",
    "SubmittedScan",
    "submit_scan",
    "synthetic_runner",
]
