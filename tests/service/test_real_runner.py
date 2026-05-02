"""Phase D, PR 1 — real-pipeline runner tests.

These tests exercise ``service.runner.make_real_runner`` /
``submit_real_scan`` end-to-end against an in-memory state store and
``MockImmichClient``. The real ONNX session and authenticated Immich
client paths land in PR 2 / PR 3; here we validate the wiring,
the multi-tenant invariants, the ``no_active_model`` refusal, and the
privacy constraints required by the Phase D plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import pytest

from mediarefinery.classifier import (
    ClassifierInput,
    ConfiguredClassifier,
    NoopClassifier,
    RawModelOutput,
)
from mediarefinery.config import AppConfig, Category, ClassifierProfile
from mediarefinery.immich import AssetRef, MockImmichClient
from mediarefinery.service.runner import (
    RunnerFactories,
    default_factories,
    make_real_runner,
    submit_real_scan,
    synthesize_app_config,
)
from mediarefinery.service.scheduler import ScanRejected
from mediarefinery.service.state_v2 import StateStoreV2


def _seed_user(store: StateStoreV2, user_id: str = "user-a") -> None:
    store.upsert_user(user_id=user_id, email=f"{user_id}@x.invalid")


def _seed_active_model(store: StateStoreV2, sha: str = "a" * 64) -> None:
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO model_registry(name, version, sha256, active) VALUES (?,?,?,1)",
        ("test-model", "1.0", sha),
    )
    store._conn.commit()  # type: ignore[attr-defined]


def _make_assets(n: int = 2) -> list[AssetRef]:
    now = datetime.now(timezone.utc)
    return [
        AssetRef(
            asset_id=f"asset-{i}",
            media_type="image",
            checksum=f"sum-{i}",
            created_at=now,
            updated_at=now,
        )
        for i in range(n)
    ]


def _factories_with(
    *,
    assets: Sequence[AssetRef],
    classifier_label: str,
    config_factory=synthesize_app_config,
) -> RunnerFactories:
    def immich_factory(_user_id):
        return MockImmichClient(assets=list(assets))

    def classifier_factory(_sha):
        profile = ClassifierProfile(
            name="test",
            backend="noop",
            model_path=None,
            output_mapping={classifier_label: classifier_label},
        )
        cfg = AppConfig(
            source=None,
            raw={
                "version": 1,
                "categories": [{"id": classifier_label}],
                "classifier_profiles": {"test": {"backend": "noop"}},
                "classifier": {"active_profile": "test"},
            },
            categories=(Category(id=classifier_label),),
            classifier_profiles={"test": profile},
            active_profile_name="test",
        )
        return NoopClassifier(cfg)

    return RunnerFactories(
        immich_factory=immich_factory,
        classifier_factory=classifier_factory,
        config_factory=config_factory,
    )


def test_submit_real_scan_refuses_when_no_active_model(tmp_path):
    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        _seed_user(store)
        with pytest.raises(ScanRejected) as exc:
            submit_real_scan(store=store, user_id="user-a")
        assert exc.value.reason == "no_active_model"


def test_submit_real_scan_runs_pipeline_against_mock_immich(tmp_path):
    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        _seed_user(store)
        _seed_active_model(store)
        scoped = store.with_user("user-a")
        scoped.set_categories({"nsfw": {"id": "nsfw"}})
        scoped.set_policies(
            {"nsfw": {"image": {"on_match": ["add_tag"]}}}
        )

        assets = _make_assets(3)
        factories = _factories_with(assets=assets, classifier_label="nsfw")

        submitted = submit_real_scan(
            store=store, user_id="user-a", factories=factories
        )

        # Wait briefly for daemon thread to finish.
        import time

        for _ in range(50):
            row = scoped.get_run(submitted.run_id)
            if row is not None and row["status"] != "running":
                break
            time.sleep(0.05)

        run = scoped.get_run(submitted.run_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["dry_run"] == 1

        actions = scoped.list_actions()
        assert len(actions) == 3
        assert all(a["dry_run"] == 1 for a in actions)
        # add_tag is unsupported by MockImmichClient capabilities by
        # default → ActionExecutor records tag_unsupported but
        # success=False; for PR 1 we only assert rows were persisted
        # and the dry_run flag is honoured.

        audit = [a["action"] for a in scoped.list_audit()]
        assert "scan.start" in audit
        assert "scan.finish" in audit


def test_runner_two_user_isolation(tmp_path):
    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        _seed_user(store, "user-a")
        _seed_user(store, "user-b")
        _seed_active_model(store)
        for uid in ("user-a", "user-b"):
            sc = store.with_user(uid)
            sc.set_categories({"x": {"id": "x"}})
            sc.set_policies({"x": {"image": {"on_match": ["no_action"]}}})

        runner = make_real_runner(
            _factories_with(assets=_make_assets(2), classifier_label="x")
        )

        scoped_a = store.with_user("user-a")
        run_a = scoped_a.start_run(dry_run=True, command="scan")
        runner(store, "user-a", run_a)

        # user-b sees nothing from user-a's run
        scoped_b = store.with_user("user-b")
        assert scoped_b.list_actions() == []
        assert scoped_b.list_runs() == []
        assert scoped_b.list_assets() == []
        assert scoped_b.list_audit() == []

        # user-a sees its own rows
        assert len(scoped_a.list_actions()) == 2
        assert len(scoped_a.list_assets()) == 2


def test_synthesize_app_config_falls_back_to_uncategorised(tmp_path):
    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        _seed_user(store)
        scoped = store.with_user("user-a")
        cfg = synthesize_app_config(scoped)
        assert cfg.active_profile_name == "service-default"
        assert cfg.category_ids == {"uncategorised"}
        assert cfg.actions["dry_run"] is True


def test_default_factories_run_without_assets(tmp_path):
    """Smoke: default factories (Mock client with no seeded assets,
    NoopClassifier placeholder) execute an empty scan cleanly."""

    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        _seed_user(store)
        _seed_active_model(store)
        runner = make_real_runner(default_factories())
        scoped = store.with_user("user-a")
        run_id = scoped.start_run(dry_run=True, command="scan")
        runner(store, "user-a", run_id)
        run = scoped.get_run(run_id)
        assert run is not None
        assert run["status"] == "completed"
        assert scoped.list_actions() == []
