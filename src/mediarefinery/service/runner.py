"""Real-pipeline runner for v2 service mode (Phase D, PR 1).

Replaces ``synthetic_runner`` with a runner callable that drives the
v1 pipeline modules (``AssetScanner`` → ``MediaExtractor`` →
``ConfiguredClassifier`` → ``DecisionEngine`` → ``ActionExecutor``)
and persists per-asset rows into the v2 multi-tenant state store.

Three injectable factories isolate the network and ONNX surfaces so
later Phase D PRs can swap in real implementations without touching
this module:

- ``immich_factory(user_id) -> ImmichClient`` — PR 3 returns an
  authenticated client built from the user's stored API key. PR 1
  defaults to ``MockImmichClient`` so the pipeline is exercisable
  end-to-end without network access.
- ``classifier_factory(active_model_sha256) -> ConfiguredClassifier``
  — PR 2 returns a process-cached ``onnxruntime`` session keyed on
  the active model. PR 1 defaults to ``NoopClassifier``.
- ``config_factory(scoped_state) -> AppConfig`` — synthesises an
  ``AppConfig`` from per-user categories/policies persisted in
  ``user_config``. PR 1 forces ``actions.dry_run=true``; PR 3 flips
  it once locked-folder writes are wired.

``submit_real_scan`` is the entry point the FastAPI ``/scans``
router will call in PR 5; it enforces the ``no_active_model``
refusal documented in the Phase D plan and otherwise delegates to
``scheduler.submit_scan`` with the constructed runner.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from ..actions import ActionExecutor
from ..classifier import (
    ClassifierError,
    ClassifierInput,
    ConfiguredClassifier,
    NoopClassifier,
)
from ..config import AppConfig, Category, ClassifierProfile
from ..decision import DecisionEngine
from ..extractor import MediaExtractionError, MediaExtractor
from ..immich import AssetRef, ImmichClient, MockImmichClient
from ..scanner import AssetScanner
from .scheduler import ScanRejected, SubmittedScan, submit_scan
from .state_v2 import StateStoreV2, UserScopedState

log = logging.getLogger("mediarefinery.service.runner")


ImmichFactory = Callable[[str], ImmichClient]
ClassifierFactory = Callable[[str | None], ConfiguredClassifier]
ConfigFactory = Callable[[UserScopedState], AppConfig]


@dataclass(frozen=True)
class RunnerFactories:
    """Injection seams for ``make_real_runner``.

    Tests pass deterministic stubs; PR 2/3 swap in the real ONNX +
    authenticated-Immich implementations.
    """

    immich_factory: ImmichFactory
    classifier_factory: ClassifierFactory
    config_factory: ConfigFactory


def synthesize_app_config(scoped: UserScopedState) -> AppConfig:
    """Build an in-memory ``AppConfig`` from a user's stored config.

    Policies and categories live in ``user_config`` (Phase B); the
    pipeline modules consume an ``AppConfig`` so we synthesise one
    here. Sensitive defaults: ``actions.dry_run=true`` is forced — PR
    3 introduces a per-action gate before flipping live writes on.
    """

    persisted = scoped.get_config()
    raw_categories = persisted.get("categories") or {}
    raw_policies = persisted.get("policies") or {}

    category_ids = sorted(str(cid) for cid in raw_categories.keys()) or [
        "uncategorised"
    ]
    output_mapping = {cid: cid for cid in category_ids}
    profile = ClassifierProfile(
        name="service-default",
        backend="noop",
        model_path=None,
        output_mapping=output_mapping,
    )
    categories = tuple(Category(id=cid) for cid in category_ids)

    raw: dict[str, Any] = {
        "version": 1,
        "categories": [{"id": cid} for cid in category_ids],
        "classifier_profiles": {"service-default": {"backend": "noop"}},
        "classifier": {"active_profile": "service-default"},
        "scanner": {"mode": "full", "media_types": ["image"]},
        "actions": {"dry_run": True},
        "policies": dict(raw_policies),
        "video": {},
        "runtime": {},
        "state": {},
    }

    return AppConfig(
        source=None,
        raw=raw,
        categories=categories,
        classifier_profiles={"service-default": profile},
        active_profile_name="service-default",
    )


def _default_immich_factory(_user_id: str) -> ImmichClient:
    return MockImmichClient(assets=[])


def _default_classifier_factory(_active_sha: str | None) -> ConfiguredClassifier:
    placeholder_config = synthesize_app_config_placeholder()
    return NoopClassifier(placeholder_config)


def synthesize_app_config_placeholder() -> AppConfig:
    """Stand-in ``AppConfig`` used only to construct ``NoopClassifier``
    when the runner has not yet seen the per-user config.

    The classifier profile's ``output_mapping`` matters for the noop
    backend; the categories are overwritten by the real config built
    inside the runner.
    """

    profile = ClassifierProfile(
        name="service-default",
        backend="noop",
        model_path=None,
        output_mapping={"uncategorised": "uncategorised"},
    )
    return AppConfig(
        source=None,
        raw={
            "version": 1,
            "categories": [{"id": "uncategorised"}],
            "classifier_profiles": {"service-default": {"backend": "noop"}},
            "classifier": {"active_profile": "service-default"},
        },
        categories=(Category(id="uncategorised"),),
        classifier_profiles={"service-default": profile},
        active_profile_name="service-default",
    )


def default_factories() -> RunnerFactories:
    return RunnerFactories(
        immich_factory=_default_immich_factory,
        classifier_factory=_default_classifier_factory,
        config_factory=synthesize_app_config,
    )


def make_real_runner(
    factories: RunnerFactories | None = None,
    *,
    dry_run: bool = True,
) -> Callable[[StateStoreV2, str, int], None]:
    """Return a runner callable compatible with ``submit_scan(runner=...)``.

    ``dry_run=True`` (default, PR 1 contract) records intended actions
    but issues no Immich writes. ``dry_run=False`` lets the executor
    perform live mutations — currently exercised by the Phase D e2e.
    """

    f = factories or default_factories()

    def _runner(store: StateStoreV2, user_id: str, run_id: int) -> None:
        scoped = store.with_user(user_id)
        active_sha = store.active_model_sha256()
        processed = 0
        errors = 0
        action_count = 0
        try:
            config = f.config_factory(scoped)
            client = f.immich_factory(user_id)
            classifier = f.classifier_factory(active_sha)
            extractor = MediaExtractor()
            decisions = DecisionEngine(config)
            executor = ActionExecutor(
                config, client, dry_run_override=dry_run
            )
            scanner = AssetScanner(config, client)

            for asset in scanner.iter_candidates():
                try:
                    classifier_input = _build_classifier_input(
                        asset, client, extractor
                    )
                    result = classifier.predict_one(classifier_input)
                except (MediaExtractionError, ClassifierError) as exc:
                    errors += 1
                    scoped.upsert_asset(
                        asset_id=asset.asset_id,
                        media_type=asset.media_type,
                        checksum=asset.checksum,
                    )
                    scoped.record_error(
                        run_id=run_id,
                        asset_id=asset.asset_id,
                        stage="extractor"
                        if isinstance(exc, MediaExtractionError)
                        else "classifier",
                        message_code=getattr(exc, "message_code", None)
                        or "pipeline_error",
                    )
                    continue

                scoped.upsert_asset(
                    asset_id=asset.asset_id,
                    media_type=asset.media_type,
                    checksum=asset.checksum,
                )
                plan = decisions.decide(
                    result.category_id,
                    asset.media_type,
                    dry_run=dry_run,
                    asset_id=asset.asset_id,
                )
                for action_result in executor.execute(plan):
                    scoped.record_action(
                        run_id=run_id,
                        asset_id=asset.asset_id,
                        action_name=action_result.action_name,
                        dry_run=action_result.dry_run,
                        would_apply=action_result.would_apply,
                        success=action_result.success,
                        error_code=action_result.error_code,
                    )
                    action_count += 1
                    # Phase D: every locked-folder write or attempt is
                    # an audit event. Asset id is logged; no asset bytes
                    # ever reach the audit table.
                    if action_result.action_name == "move_to_locked_folder":
                        scoped.write_audit(
                            action="asset.locked"
                            if action_result.success and not action_result.dry_run
                            else "asset.locked.attempt",
                            target_asset_id=asset.asset_id,
                            run_id=run_id,
                            after_state="locked"
                            if action_result.success and not action_result.dry_run
                            else None,
                        )
                processed += 1

            scoped.write_audit(action="scan.finish", run_id=run_id)
            scoped.finish_run(
                run_id,
                status="completed",
                summary_json=json.dumps(
                    {
                        "processed": processed,
                        "errors": errors,
                        "actions": action_count,
                        "dry_run": dry_run,
                        "model_sha256": active_sha,
                    },
                    sort_keys=True,
                ),
            )
        except Exception:
            log.exception(
                "real scan runner failed",
                extra={"user_id": user_id, "run_id": run_id},
            )
            scoped.write_audit(action="scan.failed", run_id=run_id)
            scoped.finish_run(run_id, status="failed")

    return _runner


def _build_classifier_input(
    asset: AssetRef,
    client: ImmichClient,
    extractor: MediaExtractor,
) -> ClassifierInput:
    if asset.media_type != "image":
        return extractor.image_input(
            asset_id=asset.asset_id,
            media_type=asset.media_type,
            image_bytes=None,
            metadata=dict(asset.metadata),
        )
    preview = client.get_preview_bytes(asset.asset_id)
    return extractor.image_input(
        asset_id=asset.asset_id,
        media_type=asset.media_type,
        image_bytes=preview,
        metadata=dict(asset.metadata),
        source="preview",
    )


def submit_real_scan(
    *,
    store: StateStoreV2,
    user_id: str,
    factories: RunnerFactories | None = None,
    daily_quota: int | None = None,
    dry_run: bool = True,
) -> SubmittedScan:
    """Phase-D entry point: refuse if no model is active, otherwise
    enqueue a real-pipeline scan via :func:`scheduler.submit_scan`.
    """

    if store.active_model_sha256() is None:
        raise ScanRejected("no_active_model")
    runner = make_real_runner(factories, dry_run=dry_run)
    kwargs: dict[str, Any] = {"store": store, "user_id": user_id, "runner": runner}
    if daily_quota is not None:
        kwargs["daily_quota"] = daily_quota
    return submit_scan(**kwargs)


__all__ = [
    "ConfigFactory",
    "ClassifierFactory",
    "ImmichFactory",
    "RunnerFactories",
    "default_factories",
    "make_real_runner",
    "submit_real_scan",
    "synthesize_app_config",
]


# Silence linters in environments that strip unused-but-documented imports.
_ = (Iterable, field, replace)
