from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig, DESTRUCTIVE_ACTIONS
from .decision import SIDE_EFFECT_ACTIONS, ActionPlan
from .immich import ImmichCapabilities, ImmichClient


RECORD_ONLY_ACTIONS = frozenset({"no_action", "manual_review"})
SUPPORTED_EXECUTOR_ACTIONS = RECORD_ONLY_ACTIONS | SIDE_EFFECT_ACTIONS


@dataclass(frozen=True)
class ActionExecutionResult:
    action_name: str
    dry_run: bool
    would_apply: bool
    success: bool
    error_code: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_name": self.action_name,
            "dry_run": self.dry_run,
            "would_apply": self.would_apply,
            "success": self.success,
            "error_code": self.error_code,
        }


class ActionExecutor:
    """Apply validated action plans through guarded Immich client methods."""

    def __init__(
        self,
        config: AppConfig,
        client: ImmichClient,
        *,
        dry_run_override: bool | None = None,
    ):
        self._actions = config.actions
        self._client = client
        self._configured_dry_run = bool(self._actions.get("dry_run", True))
        self._live_actions_enabled = (
            self._configured_dry_run is False or dry_run_override is False
        )

    def execute(self, plan: ActionPlan) -> tuple[ActionExecutionResult, ...]:
        return tuple(
            self._execute_one(
                plan,
                action.name,
                would_apply=action.would_apply,
            )
            for action in plan.intended_actions
        )

    def _execute_one(
        self,
        plan: ActionPlan,
        action_name: str,
        *,
        would_apply: bool,
    ) -> ActionExecutionResult:
        normalized_action = action_name.strip().lower().replace("-", "_")
        if normalized_action in DESTRUCTIVE_ACTIONS:
            return self._failure(
                plan,
                action_name,
                would_apply=False,
                error_code="destructive_action_unsupported",
                message="destructive actions are not supported",
            )
        if action_name not in SUPPORTED_EXECUTOR_ACTIONS:
            return self._failure(
                plan,
                action_name,
                would_apply=False,
                error_code="unsupported_action",
                message="action is not supported",
            )

        if action_name in RECORD_ONLY_ACTIONS:
            return ActionExecutionResult(
                action_name=action_name,
                dry_run=plan.dry_run,
                would_apply=False,
                success=True,
                error_code=plan.error_code,
                message=plan.reason if plan.error_code is not None else None,
            )

        if plan.dry_run:
            return ActionExecutionResult(
                action_name=action_name,
                dry_run=True,
                would_apply=would_apply,
                success=True,
            )

        if not self._live_actions_enabled:
            return self._failure(
                plan,
                action_name,
                would_apply=would_apply,
                error_code="live_actions_not_enabled",
                message="live actions require actions.dry_run=false",
            )

        if plan.asset_id is None:
            return self._failure(
                plan,
                action_name,
                would_apply=would_apply,
                error_code="missing_asset_id",
                message="action plan is missing asset_id",
            )

        if action_name == "add_to_review_album":
            return self._add_to_review_album(plan, action_name)
        if action_name == "add_tag":
            return self._add_tag(plan, action_name)
        if action_name == "archive":
            return self._archive(plan, action_name)
        if action_name == "move_to_locked_folder":
            return self._move_to_locked_folder(plan, action_name)

        return self._failure(
            plan,
            action_name,
            would_apply=False,
            error_code="unsupported_action",
            message="action is not supported",
        )

    def _add_to_review_album(
        self,
        plan: ActionPlan,
        action_name: str,
    ) -> ActionExecutionResult:
        album_name = str(self._actions.get("review_album_name") or "").strip()
        if not album_name:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="review_album_name_missing",
                message="review album name is missing",
            )

        try:
            if bool(self._actions.get("create_album_if_missing", True)):
                album_id = self._client.create_or_get_album(album_name)
            else:
                album_id = self._client.find_album_by_name(album_name)
                if album_id is None:
                    return self._failure(
                        plan,
                        action_name,
                        would_apply=True,
                        error_code="review_album_missing",
                        message="review album does not exist",
                    )
            self._client.add_to_album(album_id, [plan.asset_id])
        except Exception:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="album_action_failed",
                message="review album action failed",
            )

        return self._success(plan, action_name, would_apply=True)

    def _add_tag(
        self,
        plan: ActionPlan,
        action_name: str,
    ) -> ActionExecutionResult:
        if not _capabilities(self._client).tags:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="tag_unsupported",
                message="Immich tag actions are unsupported by this client",
            )

        tag_name = str(self._actions.get("tag_name") or "").strip()
        if not tag_name:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="tag_name_missing",
                message="tag name is missing",
            )

        try:
            if bool(self._actions.get("create_tag_if_missing", True)):
                tag_id = self._client.create_or_get_tag(tag_name)
            else:
                tag_id = self._client.find_tag_by_name(tag_name)
                if tag_id is None:
                    return self._failure(
                        plan,
                        action_name,
                        would_apply=True,
                        error_code="tag_missing",
                        message="tag does not exist",
                    )
            self._client.add_tag_to_asset(plan.asset_id, tag_id)
        except Exception:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="tag_action_failed",
                message="tag action failed",
            )

        return self._success(plan, action_name, would_apply=True)

    def _archive(
        self,
        plan: ActionPlan,
        action_name: str,
    ) -> ActionExecutionResult:
        # Phase D deprecation: ``archive`` is superseded by
        # ``move_to_locked_folder`` for v2. The HTTP adapter does not
        # implement archive_asset, so the capability check below is the
        # only path live actions take. The action stays in the
        # validation vocabulary for v1 backward compatibility; new
        # configs should use ``move_to_locked_folder`` instead.
        if self._actions.get("archive_enabled") is not True:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="archive_disabled",
                message="archive requires actions.archive_enabled=true",
            )
        if not _capabilities(self._client).archive:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="archive_unsupported",
                message="Immich archive actions are unsupported by this client",
            )

        try:
            self._client.archive_asset(plan.asset_id)
        except Exception:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="archive_action_failed",
                message="archive action failed",
            )

        return self._success(plan, action_name, would_apply=True)

    def _success(
        self,
        plan: ActionPlan,
        action_name: str,
        *,
        would_apply: bool,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            action_name=action_name,
            dry_run=plan.dry_run,
            would_apply=would_apply,
            success=True,
        )

    def _failure(
        self,
        plan: ActionPlan,
        action_name: str,
        *,
        would_apply: bool,
        error_code: str,
        message: str,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            action_name=action_name,
            dry_run=plan.dry_run,
            would_apply=would_apply,
            success=False,
            error_code=error_code,
            message=message,
        )


    def _move_to_locked_folder(
        self,
        plan: ActionPlan,
        action_name: str,
    ) -> ActionExecutionResult:
        if not _capabilities(self._client).locked_folder:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="locked_folder_unsupported",
                message="Immich locked-folder actions are unsupported by this client",
            )
        try:
            self._client.set_asset_visibility(plan.asset_id, "locked")
        except Exception:
            return self._failure(
                plan,
                action_name,
                would_apply=True,
                error_code="locked_folder_action_failed",
                message="locked-folder action failed",
            )
        return self._success(plan, action_name, would_apply=True)


def _capabilities(client: ImmichClient) -> ImmichCapabilities:
    capabilities = getattr(client, "capabilities", ImmichCapabilities())
    if callable(capabilities):
        capabilities = capabilities()
    if isinstance(capabilities, ImmichCapabilities):
        return capabilities
    return ImmichCapabilities(
        albums=bool(getattr(capabilities, "albums", True)),
        tags=bool(getattr(capabilities, "tags", False)),
        archive=bool(getattr(capabilities, "archive", False)),
        locked_folder=bool(getattr(capabilities, "locked_folder", False)),
    )
