"""Phase D, PR 3 — move_to_locked_folder action + Immich client method."""

from __future__ import annotations

import pytest

from mediarefinery.actions import ActionExecutor
from mediarefinery.config import (
    ALLOWED_ACTIONS,
    AppConfig,
    Category,
    ClassifierProfile,
)
from mediarefinery.decision import SIDE_EFFECT_ACTIONS, ActionPlan
from mediarefinery.immich import (
    AssetRef,
    ImmichCapabilities,
    MockImmichClient,
)


def _config(*, dry_run: bool) -> AppConfig:
    profile = ClassifierProfile(
        name="t",
        backend="noop",
        model_path=None,
        output_mapping={"nsfw": "nsfw"},
    )
    return AppConfig(
        source=None,
        raw={
            "version": 1,
            "categories": [{"id": "nsfw"}],
            "classifier_profiles": {"t": {"backend": "noop"}},
            "classifier": {"active_profile": "t"},
            "actions": {"dry_run": dry_run},
            "policies": {
                "nsfw": {"image": {"on_match": ["move_to_locked_folder"]}}
            },
        },
        categories=(Category(id="nsfw"),),
        classifier_profiles={"t": profile},
        active_profile_name="t",
    )


def test_locked_folder_in_allowed_actions_and_side_effects():
    assert "move_to_locked_folder" in ALLOWED_ACTIONS
    assert "move_to_locked_folder" in SIDE_EFFECT_ACTIONS


def test_executor_move_to_locked_folder_capability_gated():
    cfg = _config(dry_run=False)
    client = MockImmichClient(
        assets=[AssetRef(asset_id="a-1", media_type="image")],
        capabilities=ImmichCapabilities(locked_folder=False),
    )
    executor = ActionExecutor(cfg, client, dry_run_override=False)
    plan = ActionPlan(
        category_id="nsfw",
        media_type="image",
        actions=("move_to_locked_folder",),
        dry_run=False,
        asset_id="a-1",
    )
    [result] = executor.execute(plan)
    assert result.success is False
    assert result.error_code == "locked_folder_unsupported"


def test_executor_move_to_locked_folder_writes_visibility():
    cfg = _config(dry_run=False)
    client = MockImmichClient(
        assets=[AssetRef(asset_id="a-1", media_type="image")],
        capabilities=ImmichCapabilities(locked_folder=True),
    )
    executor = ActionExecutor(cfg, client, dry_run_override=False)
    plan = ActionPlan(
        category_id="nsfw",
        media_type="image",
        actions=("move_to_locked_folder",),
        dry_run=False,
        asset_id="a-1",
    )
    [result] = executor.execute(plan)
    assert result.success is True
    assert result.error_code is None
    assert client.visibility_requests == [
        {"asset_id": "a-1", "visibility": "locked"}
    ]


def test_executor_dry_run_does_not_call_visibility():
    cfg = _config(dry_run=True)
    client = MockImmichClient(
        assets=[AssetRef(asset_id="a-1", media_type="image")],
        capabilities=ImmichCapabilities(locked_folder=True),
    )
    executor = ActionExecutor(cfg, client, dry_run_override=True)
    plan = ActionPlan(
        category_id="nsfw",
        media_type="image",
        actions=("move_to_locked_folder",),
        dry_run=True,
        asset_id="a-1",
    )
    [result] = executor.execute(plan)
    assert result.success is True
    assert result.dry_run is True
    assert client.visibility_requests == []


def test_mock_set_asset_visibility_round_trip():
    client = MockImmichClient(
        assets=[AssetRef(asset_id="a-1", media_type="image")],
        capabilities=ImmichCapabilities(locked_folder=True),
    )
    client.set_asset_visibility("a-1", "locked")
    client.set_asset_visibility("a-1", "timeline")
    assert client.visibility_requests == [
        {"asset_id": "a-1", "visibility": "locked"},
        {"asset_id": "a-1", "visibility": "timeline"},
    ]
    with pytest.raises(ValueError):
        client.set_asset_visibility("a-1", "invalid")
    with pytest.raises(KeyError):
        client.set_asset_visibility("missing", "locked")


def test_runner_audits_locked_folder_actions(tmp_path):
    """The service runner writes an audit event for every locked-folder
    write attempt (PR 3 privacy contract)."""

    from mediarefinery.classifier import NoopClassifier
    from mediarefinery.service.runner import (
        RunnerFactories,
        make_real_runner,
    )
    from mediarefinery.service.state_v2 import StateStoreV2

    db = tmp_path / "state-v2.db"
    with StateStoreV2(db) as store:
        store.upsert_user(user_id="user-a", email="a@x.invalid")
        store._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO model_registry(name, version, sha256, active) VALUES (?,?,?,1)",
            ("m", "1", "a" * 64),
        )
        store._conn.commit()  # type: ignore[attr-defined]
        scoped = store.with_user("user-a")
        scoped.set_categories({"nsfw": {"id": "nsfw"}})
        scoped.set_policies(
            {"nsfw": {"image": {"on_match": ["move_to_locked_folder"]}}}
        )

        def immich_factory(_uid):
            return MockImmichClient(
                assets=[AssetRef(asset_id="a-1", media_type="image")],
                capabilities=ImmichCapabilities(locked_folder=True),
            )

        def classifier_factory(_sha):
            profile = ClassifierProfile(
                name="t",
                backend="noop",
                model_path=None,
                output_mapping={"nsfw": "nsfw"},
            )
            cfg = AppConfig(
                source=None,
                raw={
                    "version": 1,
                    "categories": [{"id": "nsfw"}],
                    "classifier_profiles": {"t": {"backend": "noop"}},
                    "classifier": {"active_profile": "t"},
                },
                categories=(Category(id="nsfw"),),
                classifier_profiles={"t": profile},
                active_profile_name="t",
            )
            return NoopClassifier(cfg)

        from mediarefinery.service.runner import synthesize_app_config

        factories = RunnerFactories(
            immich_factory=immich_factory,
            classifier_factory=classifier_factory,
            config_factory=synthesize_app_config,
        )
        runner = make_real_runner(factories)
        run_id = scoped.start_run(dry_run=True, command="scan")
        runner(store, "user-a", run_id)

        audit_actions = [a["action"] for a in scoped.list_audit()]
        # Dry-run path emits asset.locked.attempt (not asset.locked).
        assert "asset.locked.attempt" in audit_actions
        assert "asset.locked" not in audit_actions
        assert "scan.finish" in audit_actions
