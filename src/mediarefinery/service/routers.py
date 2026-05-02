"""HTTP routers for the v2 service.

PR 4 lands ``auth``, ``me``, and ``health``. Scans, audit, categories,
policies, models, and undo arrive in PR 5.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from . import auth as _auth
from . import locked_folder as _locked_folder
from . import model_catalog as _catalog
from . import model_lifecycle as _lifecycle
from . import runner as _runner
from . import scheduler as _scheduler
from .config import ServiceConfig
from .deps import (
    client_ip,
    get_cipher,
    get_current_user,
    get_immich_client,
    get_service_config,
    get_signer,
    get_state,
    require_admin,
    require_csrf,
)
from .security import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AesGcmCipher,
    InMemoryRateLimiter,
    SessionCookieSigner,
    issue_csrf_token,
)
from .state_v2 import StateStoreV2

log = logging.getLogger("mediarefinery.service")


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=4096)


class MeResponse(BaseModel):
    user_id: str
    email: str
    name: str | None = None
    is_admin: bool


class CategoriesPayload(BaseModel):
    categories: dict


class PoliciesPayload(BaseModel):
    policies: dict


class LockedFolderUnlockPayload(BaseModel):
    run_id: int = Field(..., gt=0)
    pin: str = Field(..., min_length=1, max_length=64)


class ApiKeyPayload(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=4096)
    label: str | None = Field(None, max_length=128)


class ScanResponse(BaseModel):
    run_id: int
    status: str


class InstallModelPayload(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    license_accepted: bool = Field(...)


def _set_auth_cookies(
    response: Response,
    *,
    config: ServiceConfig,
    signed_session: str,
    csrf: str,
    ttl_seconds: int,
) -> None:
    cookie_kwargs = {
        "secure": config.cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": ttl_seconds,
    }
    response.set_cookie(
        SESSION_COOKIE_NAME,
        signed_session,
        httponly=True,
        **cookie_kwargs,
    )
    # CSRF cookie must be readable by JS to be echoed in the X-CSRF-Token
    # header (double-submit pattern), so HttpOnly is intentionally false.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf,
        httponly=False,
        **cookie_kwargs,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def build_auth_router() -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login", status_code=status.HTTP_200_OK)
    def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        state: Annotated[StateStoreV2, Depends(get_state)],
        cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
        signer: Annotated[SessionCookieSigner, Depends(get_signer)],
        config: Annotated[ServiceConfig, Depends(get_service_config)],
        immich: Annotated[httpx.Client, Depends(get_immich_client)],
    ) -> dict:
        ip = client_ip(request, config)
        limiter: InMemoryRateLimiter = request.app.state.login_limiter
        if not limiter.check(ip):
            log.warning("login rate-limited", extra={"event": "login.ratelimited", "ip": ip})
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts",
            )

        try:
            result = _auth.proxy_login(
                immich_base_url=config.immich_base_url,
                email=body.email,
                password=body.password,
                client=immich,
            )
        except _auth.InvalidCredentials:
            log.info("login rejected", extra={"event": "login.rejected", "ip": ip})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
        except _auth.AuthError as exc:
            log.error("login upstream failure", extra={"event": "login.upstream_error"})
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, detail="upstream Immich unreachable"
            ) from exc

        # First-user-becomes-admin bootstrap: if no admin exists yet,
        # promote this login to admin regardless of Immich isAdmin.
        # Subsequent logins inherit Immich isAdmin.
        promote_first_admin = state.admin_count() == 0
        state.upsert_user(
            user_id=result.user_id,
            email=result.email,
            name=result.name,
            is_admin=result.is_admin or promote_first_admin,
        )
        if promote_first_admin and not result.is_admin:
            state.promote_to_admin(result.user_id)
            log.info(
                "first user promoted to admin",
                extra={"event": "bootstrap.first_admin", "user_id": result.user_id},
            )

        session_id = _auth.mint_session_id()
        encrypted = cipher.encrypt(result.access_token.encode("utf-8"))
        expires_at = _auth.session_expiry(ttl_seconds=config.session_ttl_seconds)
        _auth.persist_session(
            conn=state._conn,
            user_id=result.user_id,
            session_id=session_id,
            encrypted_token=encrypted,
            expires_at=expires_at,
        )
        scoped = state.with_user(result.user_id)
        scoped.write_audit(action="login")

        signed = signer.sign(session_id)
        csrf = issue_csrf_token()
        _set_auth_cookies(
            response,
            config=config,
            signed_session=signed,
            csrf=csrf,
            ttl_seconds=config.session_ttl_seconds,
        )
        log.info(
            "login ok",
            extra={"event": "login.ok", "user_id": result.user_id, "ip": ip},
        )
        return {
            "user_id": result.user_id,
            "email": result.email,
            "name": result.name,
            "is_admin": result.is_admin,
        }

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        request: Request,
        response: Response,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
        cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
        config: Annotated[ServiceConfig, Depends(get_service_config)],
        immich: Annotated[httpx.Client, Depends(get_immich_client)],
    ) -> Response:
        session_id = request.state.session_id
        row = _auth.lookup_session(conn=state._conn, session_id=session_id)
        if row is not None:
            try:
                token = _auth.decrypt_session_token(cipher=cipher, row=row)
                _auth.proxy_logout(
                    immich_base_url=config.immich_base_url,
                    access_token=token,
                    client=immich,
                )
            except ValueError:
                pass  # encrypted token unreadable; revoke our row anyway
        _auth.revoke_session(conn=state._conn, session_id=session_id)
        scoped = state.with_user(user_id)
        scoped.write_audit(action="logout")
        _clear_auth_cookies(response)
        log.info("logout ok", extra={"event": "logout.ok", "user_id": user_id})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def build_me_router() -> APIRouter:
    router = APIRouter(tags=["me"])

    @router.get("/me", response_model=MeResponse)
    def me(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> MeResponse:
        cursor = state._conn.execute(
            "SELECT user_id, email, name, is_admin FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
        return MeResponse(
            user_id=row["user_id"],
            email=row["email"],
            name=row["name"],
            is_admin=bool(row["is_admin"]),
        )

    @router.delete("/me", dependencies=[Depends(require_csrf)])
    def delete_me(
        request: Request,
        response: Response,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
        cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
        config: Annotated[ServiceConfig, Depends(get_service_config)],
        immich: Annotated[httpx.Client, Depends(get_immich_client)],
    ) -> Response:
        """Idempotent purge of the calling user (threat-model T20).

        Sessions, API keys, runs, actions, errors, assets, and per-user
        config rows are deleted; the encrypted Bearer / API key blobs
        are zeroed in place first so a recovered DB page does not yield
        decryptable ciphertext. Audit-log rows are anonymized in place
        by rewriting ``user_id`` to the sentinel ``"user_deleted"`` —
        the threat model accepts either delete-or-anonymize, and
        anonymize-in-place preserves audit-trail integrity. Finally the
        ``users`` row is deleted and the caller's session cookies are
        cleared.
        """

        session_id = request.state.session_id
        row = _auth.lookup_session(conn=state._conn, session_id=session_id)
        if row is not None:
            try:
                token = _auth.decrypt_session_token(cipher=cipher, row=row)
                _auth.proxy_logout(
                    immich_base_url=config.immich_base_url,
                    access_token=token,
                    client=immich,
                )
            except ValueError:
                pass  # encrypted token unreadable; purge anyway
        _auth.revoke_session(conn=state._conn, session_id=session_id)

        state.with_user(user_id).purge()
        _clear_auth_cookies(response)
        log.info("account purged", extra={"event": "me.delete", "user_id": user_id})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def build_me_config_router() -> APIRouter:
    router = APIRouter(prefix="/me", tags=["me"])

    @router.get("/categories")
    def get_categories(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        scoped = state.with_user(user_id)
        active_sha = state.active_model_sha256()
        last_seen = scoped.last_seen_model_sha256()
        needs_reclassify = bool(
            active_sha is not None
            and last_seen is not None
            and active_sha != last_seen
        )
        return {
            "categories": scoped.get_config()["categories"],
            "active_model_sha256": active_sha,
            "last_seen_model_sha256": last_seen,
            "needs_reclassify": needs_reclassify,
        }

    @router.put("/categories", dependencies=[Depends(require_csrf)])
    def put_categories(
        body: CategoriesPayload,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        scoped = state.with_user(user_id)
        scoped.set_categories(body.categories)
        scoped.write_audit(action="categories.update")
        return {"categories": scoped.get_config()["categories"]}

    @router.get("/policies")
    def get_policies(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        return {"policies": state.with_user(user_id).get_config()["policies"]}

    @router.put("/policies", dependencies=[Depends(require_csrf)])
    def put_policies(
        body: PoliciesPayload,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        scoped = state.with_user(user_id)
        scoped.set_policies(body.policies)
        scoped.write_audit(action="policies.update")
        return {"policies": scoped.get_config()["policies"]}

    @router.post("/api-key", dependencies=[Depends(require_csrf)], status_code=201)
    def put_api_key(
        body: ApiKeyPayload,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
        cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
    ) -> dict:
        scoped = state.with_user(user_id)
        encrypted = cipher.encrypt(body.api_key.encode("utf-8"))
        key_id = scoped.store_api_key(encrypted_key=encrypted, label=body.label)
        scoped.write_audit(action="api_key.store")
        return {"id": key_id, "label": body.label}

    @router.get("/api-key")
    def list_api_keys(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        rows = state.with_user(user_id).list_api_keys()
        return {
            "api_keys": [
                {"id": int(row["id"]), "label": row["label"], "created_at": row["created_at"]}
                for row in rows
            ]
        }

    @router.post(
        "/locked-folder/unlock",
        dependencies=[Depends(require_csrf)],
    )
    def unlock_locked_folder(
        body: LockedFolderUnlockPayload,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
        cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
        immich: Annotated[httpx.Client, Depends(get_immich_client)],
        config: Annotated[ServiceConfig, Depends(get_service_config)],
    ) -> dict:
        # Phase D, threat-model T09 / T10:
        # - PIN flows request -> Immich without being logged or stored.
        # - The PIN-unlocked Bearer is held in a local for the
        #   duration of this handler only and rebound to None before
        #   the response is built. It never reaches state-v2.db.
        scoped = state.with_user(user_id)
        if scoped.get_run(body.run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="scan not found")
        sessions = scoped.list_sessions()
        if not sessions:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="no active session"
            )
        # Use the most recently created session row.
        session_row = sessions[-1]
        bearer: str | None = _auth.decrypt_session_token(
            cipher=cipher, row=session_row
        )

        locked_asset_ids = [
            str(row["asset_id"])
            for row in scoped.list_actions()
            if int(row["run_id"]) == body.run_id
            and row["action_name"] == "move_to_locked_folder"
            and row["success"] == 1
        ]
        if not locked_asset_ids:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="no locked-folder actions to revert",
            )

        try:
            try:
                outcome = _locked_folder.unlock_and_revert(
                    immich_base_url=config.immich_base_url,
                    bearer=bearer,
                    pin=body.pin,
                    asset_ids=locked_asset_ids,
                    client=immich,
                )
            except _locked_folder.InvalidPin:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid pin")
            except _locked_folder.UpstreamUnavailable:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, detail="upstream Immich unreachable"
                )
            except _locked_folder.UnlockError:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, detail="upstream Immich error"
                )
        finally:
            # Defensive zeroing: rebind so the local reference goes away
            # ahead of the response serialiser. Python strings are
            # immutable so we cannot wipe the original bytes; what we
            # can guarantee is that no callsite below this comment
            # holds the value, and no caller of this endpoint ever
            # sees it.
            bearer = None

        for asset_id in locked_asset_ids:
            if asset_id in outcome.failed_asset_ids:
                continue
            scoped.write_audit(
                action="asset.unlocked",
                target_asset_id=asset_id,
                run_id=body.run_id,
                after_state="timeline",
            )
        scoped.write_audit(action="scan.undo", run_id=body.run_id)

        return {
            "run_id": body.run_id,
            "reverted": outcome.reverted_count,
            "failed_asset_ids": list(outcome.failed_asset_ids),
        }

    return router


def build_scans_router() -> APIRouter:
    router = APIRouter(prefix="/scans", tags=["scans"])

    @router.post("", dependencies=[Depends(require_csrf)], status_code=202)
    def create_scan(
        request: Request,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> ScanResponse:
        # Phase D wiring: when an active model is registered we run the
        # real pipeline; with no model installed we keep the Phase B
        # synthetic runner so contributors and CI can drive the
        # multi-tenant invariants without a model on disk.
        runner_factories = getattr(request.app.state, "runner_factories", None)
        try:
            if state.active_model_sha256() is not None:
                submitted = _runner.submit_real_scan(
                    store=state,
                    user_id=user_id,
                    factories=runner_factories,
                )
            else:
                submitted = _scheduler.submit_scan(
                    store=state, user_id=user_id
                )
        except _scheduler.ScanRejected as exc:
            if exc.reason == "concurrency_cap":
                raise HTTPException(status.HTTP_409_CONFLICT, detail="scan already running")
            if exc.reason == "daily_quota":
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS, detail="daily scan quota exceeded"
                )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.reason)
        return ScanResponse(run_id=submitted.run_id, status="running")

    @router.get("")
    def list_scans(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        rows = state.with_user(user_id).list_runs()
        return {
            "scans": [
                {
                    "run_id": int(row["id"]),
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                }
                for row in rows
            ]
        }

    @router.get("/{run_id}")
    def get_scan(
        run_id: int,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        scoped = state.with_user(user_id)
        row = scoped.get_run(run_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="scan not found")
        actions = [
            {
                "action_name": action["action_name"],
                "asset_id": action["asset_id"],
                "success": None if action["success"] is None else bool(action["success"]),
                "error_code": action["error_code"],
            }
            for action in scoped.list_actions()
            if action["run_id"] == run_id
        ]
        return {
            "run_id": int(row["id"]),
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "summary_json": row["summary_json"],
            "actions": actions,
        }

    @router.post("/{run_id}/undo", dependencies=[Depends(require_csrf)])
    def undo_scan(
        run_id: int,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        scoped = state.with_user(user_id)
        if scoped.get_run(run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="scan not found")
        try:
            reverted = scoped.revert_run_actions(run_id)
        except PermissionError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="scan not found")
        scoped.write_audit(action="scan.undo", run_id=run_id)
        return {"run_id": run_id, "reverted": reverted}

    return router


def build_audit_router() -> APIRouter:
    router = APIRouter(tags=["audit"])

    @router.get("/audit")
    def list_audit(
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        rows = state.with_user(user_id).list_audit()
        return {
            "entries": [
                {
                    "id": int(row["id"]),
                    "at": row["at"],
                    "action": row["action"],
                    "target_asset_id": row["target_asset_id"],
                    "run_id": row["run_id"],
                }
                for row in rows
            ]
        }

    return router


class BootstrapPayload(BaseModel):
    accept_terms: bool


def build_setup_router() -> APIRouter:
    router = APIRouter(prefix="/setup", tags=["setup"])

    @router.get("/bootstrap")
    def bootstrap_status(
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        terms_accepted = bool(state.get_setting("terms_accepted"))
        users_exist = bool(state.list_users())
        admin_present = state.admin_count() > 0
        return {
            "terms_accepted": terms_accepted,
            "users_exist": users_exist,
            "admin_present": admin_present,
            "ready": terms_accepted and admin_present,
        }

    @router.post("/bootstrap", status_code=status.HTTP_200_OK)
    def bootstrap(
        body: BootstrapPayload,
        request: Request,
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        # Bootstrap is unauthenticated by design — it runs on a fresh
        # container before any user exists. Once the terms are
        # recorded, the endpoint refuses re-bootstrap to avoid an
        # anonymous reset of the system.
        if state.get_setting("terms_accepted"):
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="bootstrap already completed"
            )
        if not body.accept_terms:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="terms must be accepted"
            )
        from datetime import datetime, timezone

        accepted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.set_setting(
            "terms_accepted",
            {"accepted_at": accepted_at, "remote_ip": client_ip(
                request, request.app.state.config
            )},
        )
        log.info(
            "bootstrap completed",
            extra={"event": "bootstrap.complete", "accepted_at": accepted_at},
        )
        return {"terms_accepted": True, "accepted_at": accepted_at}

    return router


def build_models_router() -> APIRouter:
    router = APIRouter(prefix="/models", tags=["models"])

    def _entry_to_dict(entry: _catalog.CatalogEntry, installed_sha: set[str]) -> dict:
        return {
            "id": entry.id,
            "name": entry.name,
            "kind": entry.kind,
            "status": entry.status,
            "license": entry.license,
            "license_url": entry.license_url,
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
            "presets": list(entry.presets),
            "installed": entry.sha256 in installed_sha,
            "installable": entry.installable,
        }

    @router.get("/catalog")
    def get_catalog(
        request: Request,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        catalog_path = getattr(request.app.state, "catalog_path", None)
        try:
            entries = _catalog.load_catalog(catalog_path)
        except _catalog.CatalogError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
        installed_sha = {
            row.sha256
            for row in _lifecycle.list_installed(
                conn=state._conn, data_dir=request.app.state.config.data_dir
            )
        }
        return {"models": [_entry_to_dict(e, installed_sha) for e in entries]}

    @router.get("")
    def get_installed(
        request: Request,
        user_id: Annotated[str, Depends(get_current_user)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        installed = _lifecycle.list_installed(
            conn=state._conn, data_dir=request.app.state.config.data_dir
        )
        return {
            "installed": [
                {
                    "id": m.id,
                    "name": m.name,
                    "version": m.version,
                    "sha256": m.sha256,
                    "license": m.license,
                    "active": m.active,
                    "present_on_disk": m.path is not None,
                }
                for m in installed
            ]
        }

    @router.post(
        "/install",
        dependencies=[Depends(require_csrf), Depends(require_admin)],
        status_code=status.HTTP_201_CREATED,
    )
    def install(
        body: InstallModelPayload,
        request: Request,
        user_id: Annotated[str, Depends(require_admin)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> dict:
        catalog_path = getattr(request.app.state, "catalog_path", None)
        try:
            entries = _catalog.load_catalog(catalog_path)
        except _catalog.CatalogError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
        entry = _catalog.find_entry(entries, body.model_id)
        if entry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown model id")
        if not entry.installable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"model {entry.id} is not installable (status={entry.status})",
            )
        try:
            installed = _lifecycle.install_model(
                entry=entry,
                data_dir=request.app.state.config.data_dir,
                conn=state._conn,
                actor_user_id=user_id,
                license_accepted=body.license_accepted,
            )
        except _lifecycle.HashMismatch as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, detail=f"hash mismatch: {exc}"
            )
        except _lifecycle.InstallError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return {
            "id": installed.id,
            "model_id": installed.version,
            "name": installed.name,
            "sha256": installed.sha256,
            "active": installed.active,
        }

    @router.delete(
        "/{registry_id}",
        dependencies=[Depends(require_csrf), Depends(require_admin)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def uninstall(
        registry_id: int,
        request: Request,
        user_id: Annotated[str, Depends(require_admin)],
        state: Annotated[StateStoreV2, Depends(get_state)],
    ) -> Response:
        try:
            _lifecycle.uninstall_model(
                registry_id=registry_id,
                data_dir=request.app.state.config.data_dir,
                conn=state._conn,
                actor_user_id=user_id,
            )
        except _lifecycle.InstallError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def build_health_router() -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @router.get("/health/ready")
    def ready(
        state: Annotated[StateStoreV2, Depends(get_state)],
        immich: Annotated[httpx.Client, Depends(get_immich_client)],
        config: Annotated[ServiceConfig, Depends(get_service_config)],
    ) -> dict:
        details = {"db": "ok", "immich": "unknown"}
        try:
            state._conn.execute("SELECT 1")
        except Exception:
            details["db"] = "fail"
        try:
            response = immich.get("/api/server/about", timeout=2.0)
            details["immich"] = "ok" if response.status_code < 500 else "fail"
        except httpx.HTTPError:
            details["immich"] = "fail"
        ok = all(value == "ok" for value in details.values())
        return {"status": "ok" if ok else "degraded", **details}

    return router


__all__ = [
    "ApiKeyPayload",
    "CategoriesPayload",
    "LoginRequest",
    "MeResponse",
    "PoliciesPayload",
    "ScanResponse",
    "BootstrapPayload",
    "build_audit_router",
    "build_auth_router",
    "build_health_router",
    "build_me_config_router",
    "build_me_router",
    "build_models_router",
    "build_scans_router",
    "build_setup_router",
]
