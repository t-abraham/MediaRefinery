from __future__ import annotations

from pathlib import Path
from typing import TextIO
import logging
import sys

from .actions import ActionExecutor, ActionExecutionResult
from .classifier import (
    ClassifierError,
    ClassifierInput,
    ClassificationResult,
    ConfiguredClassifier,
    create_classifier,
)
from .config import AppConfig
from .decision import ActionPlan, DecisionEngine
from .extractor import MediaExtractionError, MediaExtractor
from .immich import AssetRef, ImmichClient, MockImmichClient
from .observability import elapsed_ms, log_event, monotonic_time
from .reporter import Reporter, ScanSummary, summarize_scan
from .scanner import AssetScanner
from .state import StateStore


LOGGER = logging.getLogger("mediarefinery.pipeline")


def run_scan(
    config: AppConfig,
    *,
    state_path: str | Path | None = None,
    dry_run_override: bool | None = None,
    client: ImmichClient | None = None,
    stream: TextIO | None = None,
) -> ScanSummary:
    dry_run = (
        bool(dry_run_override)
        if dry_run_override is not None
        else bool(config.actions.get("dry_run", True))
    )
    sqlite_path = state_path or config.state.get("sqlite_path") or "state.sqlite3"
    immich_client = client or MockImmichClient()
    extractor = MediaExtractor()
    classifier = create_classifier(config)
    decisions = DecisionEngine(config)
    executor = ActionExecutor(
        config,
        immich_client,
        dry_run_override=dry_run_override,
    )
    reporter = Reporter()
    action_plans: list[ActionPlan] = []
    error_count = 0
    run_start = monotonic_time()
    log_event(LOGGER, "scan.start", duration_ms=0)

    with StateStore(sqlite_path) as store:
        config_snapshot_id = store.record_config_snapshot(
            config.raw,
            source=config.source,
        )
        active_profile = config.active_profile
        model_version_id = store.record_model_version(
            backend=active_profile.backend,
            profile_name=active_profile.name,
            version=classifier.backend.version,
            model_path=active_profile.model_path,
        )
        run_id = store.start_run(
            dry_run=dry_run,
            command="scan",
            config_snapshot_id=config_snapshot_id,
            model_version_id=model_version_id,
        )
        scanner = AssetScanner(config, immich_client, state=store)
        for asset in scanner.iter_candidates():
            asset_start = monotonic_time()
            try:
                result = _classify_asset(
                    asset,
                    immich_client,
                    extractor,
                    classifier,
                    config,
                )
            except MediaExtractionError as exc:
                error_count += 1
                store.upsert_asset(asset)
                store.record_error(
                    run_id=run_id,
                    asset_id=asset.asset_id,
                    stage="extractor",
                    message_code=exc.message_code,
                    message=exc.message,
                    details=exc.as_details(),
                )
                log_event(
                    LOGGER,
                    "asset.error",
                    asset_id=asset.asset_id,
                    duration_ms=elapsed_ms(asset_start),
                    error_code=exc.message_code,
                )
                continue
            except ClassifierError as exc:
                error_count += 1
                store.upsert_asset(asset)
                store.record_error(
                    run_id=run_id,
                    asset_id=asset.asset_id,
                    stage="classifier",
                    message_code="classifier_failed",
                    message=str(exc),
                    details={
                        "asset_id": asset.asset_id,
                        "media_type": asset.media_type,
                    },
                )
                log_event(
                    LOGGER,
                    "asset.error",
                    asset_id=asset.asset_id,
                    duration_ms=elapsed_ms(asset_start),
                    error_code="classifier_failed",
                )
                continue

            action_plan = decisions.decide(
                result.category_id,
                asset.media_type,
                dry_run=dry_run,
                asset_id=asset.asset_id,
            )
            store.record_classification_run(
                run_id,
                asset,
                result,
                config_snapshot_id=config_snapshot_id,
                model_version_id=model_version_id,
            )
            action_plans.append(action_plan)
            log_event(
                LOGGER,
                "asset.classified",
                asset_id=asset.asset_id,
                category_id=result.category_id,
                duration_ms=elapsed_ms(asset_start),
            )
            if action_plan.error_code is not None:
                error_count += 1
                store.record_error(
                    run_id=run_id,
                    asset_id=asset.asset_id,
                    stage="decision",
                    message_code=action_plan.error_code,
                    message=action_plan.reason,
                    details=action_plan.as_dict(),
                )
                log_event(
                    LOGGER,
                    "asset.error",
                    asset_id=asset.asset_id,
                    category_id=result.category_id,
                    duration_ms=elapsed_ms(asset_start),
                    error_code=action_plan.error_code,
                )
            action_start = monotonic_time()
            for action_result in executor.execute(action_plan):
                _record_action_result(
                    store,
                    run_id,
                    asset.asset_id,
                    action_result,
                )
                log_event(
                    LOGGER,
                    "action.result",
                    asset_id=asset.asset_id,
                    category_id=result.category_id,
                    action_name=action_result.action_name,
                    duration_ms=elapsed_ms(action_start),
                    error_code=action_result.error_code,
                )
                if action_result.success is False:
                    error_count += 1
                    store.record_error(
                        run_id=run_id,
                        asset_id=asset.asset_id,
                        stage="action",
                        message_code=action_result.error_code or "action_failed",
                        message=action_result.message,
                        details=action_result.as_dict(),
                    )

        summary = summarize_scan(
            action_plans,
            skipped=scanner.skipped_count,
            errors=error_count,
            dry_run=dry_run,
        )
        store.finish_run(
            run_id,
            status="succeeded" if summary.errors == 0 else "completed_with_errors",
            summary={
                "processed": summary.processed,
                "skipped": summary.skipped,
                "errors": summary.errors,
                "by_category": summary.by_category,
                "intended_actions": summary.intended_actions,
                "dry_run": summary.dry_run,
            },
        )
        log_event(
            LOGGER,
            "scan.finish",
            duration_ms=elapsed_ms(run_start),
            error_code="partial_failure" if summary.errors else None,
        )

    reporter.write_scan_summary(summary, stream or sys.stdout)
    return summary


def _classify_asset(
    asset: AssetRef,
    client: ImmichClient,
    extractor: MediaExtractor,
    classifier: ConfiguredClassifier,
    config: AppConfig,
) -> ClassificationResult:
    if asset.media_type == "video":
        with extractor.video_frame_inputs(
            asset_id=asset.asset_id,
            media_type=asset.media_type,
            video_path=_video_path_from_metadata(asset.metadata),
            metadata=asset.metadata,
            video_config=config.video,
            runtime_config=config.runtime,
        ) as frame_inputs:
            return classifier.predict_aggregate(
                frame_inputs,
                asset_id=asset.asset_id,
                aggregation=config.active_profile.video_aggregation,
            )

    classifier_input = _prepare_classifier_input(
        asset,
        client,
        extractor,
    )
    return classifier.predict_one(classifier_input)


def _prepare_classifier_input(
    asset: AssetRef,
    client: ImmichClient,
    extractor: MediaExtractor,
) -> ClassifierInput:
    asset_id = asset.asset_id
    media_type = asset.media_type
    metadata = dict(asset.metadata)
    if media_type != "image":
        return extractor.image_input(
            asset_id=asset_id,
            media_type=media_type,
            image_bytes=None,
            metadata=metadata,
        )

    preview_bytes = client.get_preview_bytes(asset_id)
    return extractor.image_input(
        asset_id=asset_id,
        media_type=media_type,
        image_bytes=preview_bytes,
        metadata=metadata,
        source="preview",
    )


def _video_path_from_metadata(metadata: dict[str, str]) -> str | None:
    for key in ("video_path", "local_path", "file_path", "path"):
        value = metadata.get(key)
        if value:
            return value
    return None


def _record_action_result(
    store: StateStore,
    run_id: int,
    asset_id: str,
    action_result: ActionExecutionResult,
) -> None:
    store.record_action_run(
        run_id,
        asset_id,
        action_result.action_name,
        dry_run=action_result.dry_run,
        would_apply=action_result.would_apply,
        success=action_result.success,
        error_code=action_result.error_code,
    )

