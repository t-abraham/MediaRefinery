from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
import json
import os
import shutil
import ssl
import tempfile

from .config import AppConfig, ConfigError, load_config


STATUS_OK = "OK"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

EXIT_CONFIG_OR_USER = 1
EXIT_NETWORK_OR_AUTH = 2
EXIT_DISK = 3

DEFAULT_API_KEY_ENV = "IMMICH_API_KEY"
DEFAULT_FFMPEG_PATH = "ffmpeg"
DOCTOR_NETWORK_TIMEOUT_SECONDS = 5.0

ImmichProbe = Callable[[AppConfig, str], Sequence["DoctorCheck"]]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    exit_code: int = EXIT_CONFIG_OR_USER
    details: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED


@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        for check in self.checks:
            if check.failed:
                return check.exit_code
        return 0


@dataclass(frozen=True)
class _HttpProbeResult:
    status_code: int | None
    json_data: object | None = None
    error_code: str | None = None

    @property
    def network_failed(self) -> bool:
        return self.error_code is not None


class _ImmichDoctorHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        verify_tls: bool,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._context = None if verify_tls else ssl._create_unverified_context()

    def get_json(self, endpoint: str, *, authenticated: bool) -> _HttpProbeResult:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["x-api-key"] = self._api_key
        request = Request(
            _immich_api_url(self._base_url, endpoint),
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._context,
            ) as response:
                body = response.read(65536)
        except HTTPError as exc:
            return _HttpProbeResult(status_code=exc.code)
        except (OSError, TimeoutError, URLError):
            return _HttpProbeResult(status_code=None, error_code="network_unreachable")

        json_data = None
        if body:
            try:
                json_data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                json_data = None
        return _HttpProbeResult(status_code=response.status, json_data=json_data)


def run_doctor_checks(
    config_path: str | os.PathLike[str] | None,
    *,
    environ: Mapping[str, str] | None = None,
    immich_probe: ImmichProbe | None = None,
) -> DoctorResult:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return DoctorResult(
            (
                DoctorCheck(
                    "config",
                    STATUS_FAILED,
                    "config did not load or validate",
                    details=tuple(exc.errors),
                ),
            )
        )

    env = environ if environ is not None else os.environ
    checks: list[DoctorCheck] = [
        DoctorCheck("config", STATUS_OK, "config loaded and validated")
    ]
    api_key_env = _immich_api_key_env(config)
    api_key_value = env.get(api_key_env)
    if api_key_value:
        checks.append(
            DoctorCheck(
                "environment",
                STATUS_OK,
                f"required env var {api_key_env} is set",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "environment",
                STATUS_FAILED,
                f"required env var {api_key_env} is not set",
            )
        )

    checks.append(_check_state_path(config))
    checks.append(_check_temp_path(config))
    checks.append(_check_model_path(config))
    checks.append(_check_ffmpeg(config))

    if api_key_value:
        probe = immich_probe or probe_immich
        try:
            checks.extend(probe(config, api_key_value))
        except Exception:
            checks.append(
                DoctorCheck(
                    "immich",
                    STATUS_WARNING,
                    "Immich reachability check could not complete; local checks still ran",
                    exit_code=EXIT_NETWORK_OR_AUTH,
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "immich",
                STATUS_SKIPPED,
                f"reachability skipped because {api_key_env} is not set",
            )
        )

    return DoctorResult(tuple(checks))


def probe_immich(config: AppConfig, api_key: str) -> Sequence[DoctorCheck]:
    immich = _immich_config(config)
    client = _ImmichDoctorHttpClient(
        base_url=str(immich.get("url") or ""),
        api_key=api_key,
        timeout_seconds=_doctor_timeout(immich.get("timeout_seconds")),
        verify_tls=bool(immich.get("verify_tls", True)),
    )

    reachability = _probe_reachability(client)
    checks: list[DoctorCheck] = [reachability]
    if reachability.status == STATUS_WARNING:
        checks.append(
            DoctorCheck(
                "immich capabilities",
                STATUS_SKIPPED,
                "capability checks skipped because Immich was not reachable",
            )
        )
        return tuple(checks)

    checks.append(_probe_authentication(client))
    checks.append(_probe_capabilities(client))
    return tuple(checks)


def _probe_reachability(client: _ImmichDoctorHttpClient) -> DoctorCheck:
    for endpoint in ("/server/ping", "/server/version"):
        result = client.get_json(endpoint, authenticated=False)
        if result.network_failed:
            continue
        if result.status_code is not None and 200 <= result.status_code < 500:
            if result.status_code < 300:
                return DoctorCheck(
                    "immich",
                    STATUS_OK,
                    "configured Immich server responded to a read-only check",
                )
            return DoctorCheck(
                "immich",
                STATUS_WARNING,
                "configured Immich server responded but not with a successful readiness status",
                exit_code=EXIT_NETWORK_OR_AUTH,
            )

    return DoctorCheck(
        "immich",
        STATUS_WARNING,
        "configured Immich server was not reachable; offline local checks completed",
        exit_code=EXIT_NETWORK_OR_AUTH,
    )


def _probe_authentication(client: _ImmichDoctorHttpClient) -> DoctorCheck:
    result = client.get_json("/server/about", authenticated=True)
    if result.network_failed:
        return DoctorCheck(
            "immich auth",
            STATUS_WARNING,
            "authentication check could not reach Immich; offline local checks completed",
            exit_code=EXIT_NETWORK_OR_AUTH,
        )
    if result.status_code == 401:
        return DoctorCheck(
            "immich auth",
            STATUS_FAILED,
            "Immich rejected the configured API key env var",
            exit_code=EXIT_NETWORK_OR_AUTH,
        )
    if result.status_code == 403:
        return DoctorCheck(
            "immich auth",
            STATUS_WARNING,
            "API key is present but lacks permission for the read-only auth probe",
            exit_code=EXIT_NETWORK_OR_AUTH,
        )
    if result.status_code is not None and 200 <= result.status_code < 300:
        return DoctorCheck(
            "immich auth",
            STATUS_OK,
            "API key env var was accepted by a read-only Immich endpoint",
        )
    return DoctorCheck(
        "immich auth",
        STATUS_WARNING,
        "authentication check endpoint was unavailable or unexpected",
        exit_code=EXIT_NETWORK_OR_AUTH,
    )


def _probe_capabilities(client: _ImmichDoctorHttpClient) -> DoctorCheck:
    result = client.get_json("/server/features", authenticated=False)
    capabilities = {
        "albums": "assumed",
        "tags": "unknown",
        "archive": "unknown",
    }
    if not result.network_failed and result.status_code is not None:
        if 200 <= result.status_code < 300 and result.json_data is not None:
            tags = _find_bool_feature(result.json_data, {"tag", "tags"})
            archive = _find_bool_feature(
                result.json_data,
                {"archive", "archiving", "asset_archive"},
            )
            if tags is not None:
                capabilities["tags"] = "available" if tags else "unavailable"
            if archive is not None:
                capabilities["archive"] = "available" if archive else "unavailable"
            return DoctorCheck(
                "immich capabilities",
                STATUS_OK,
                _capability_message(capabilities),
            )

    return DoctorCheck(
        "immich capabilities",
        STATUS_WARNING,
        _capability_message(capabilities),
        exit_code=EXIT_NETWORK_OR_AUTH,
    )


def _check_state_path(config: AppConfig) -> DoctorCheck:
    value = config.state.get("sqlite_path") or "state.sqlite3"
    if not isinstance(value, str) or not value.strip():
        return DoctorCheck(
            "state",
            STATUS_FAILED,
            "state.sqlite_path must be a non-empty file path",
            exit_code=EXIT_DISK,
        )
    if value == ":memory:":
        return DoctorCheck("state", STATUS_OK, "state.sqlite_path uses memory")

    path = Path(value)
    try:
        if path.exists():
            if path.is_dir():
                return DoctorCheck(
                    "state",
                    STATUS_FAILED,
                    "state.sqlite_path points to a directory, not a SQLite file",
                    exit_code=EXIT_DISK,
                )
            with path.open("r+b"):
                pass
            return DoctorCheck("state", STATUS_OK, "state.sqlite_path is writable")

        _probe_directory_writable(path.parent)
    except OSError:
        return DoctorCheck(
            "state",
            STATUS_FAILED,
            "state.sqlite_path parent is not writable",
            exit_code=EXIT_DISK,
        )
    return DoctorCheck("state", STATUS_OK, "state.sqlite_path parent is writable")


def _check_temp_path(config: AppConfig) -> DoctorCheck:
    value = config.runtime.get("temp_dir")
    if value is None or value == "":
        temp_dir = Path(tempfile.gettempdir())
    elif isinstance(value, str):
        temp_dir = Path(value)
    else:
        return DoctorCheck(
            "temp",
            STATUS_FAILED,
            "runtime.temp_dir must be a directory path when configured",
            exit_code=EXIT_DISK,
        )

    try:
        if temp_dir.exists() and not temp_dir.is_dir():
            return DoctorCheck(
                "temp",
                STATUS_FAILED,
                "runtime.temp_dir points to a file, not a directory",
                exit_code=EXIT_DISK,
            )
        _probe_directory_writable(temp_dir)
    except OSError:
        return DoctorCheck(
            "temp",
            STATUS_FAILED,
            "runtime.temp_dir is not writable",
            exit_code=EXIT_DISK,
        )
    return DoctorCheck("temp", STATUS_OK, "runtime.temp_dir is writable")


def _check_model_path(config: AppConfig) -> DoctorCheck:
    model_path = config.active_profile.model_path
    if not model_path:
        return DoctorCheck(
            "model",
            STATUS_SKIPPED,
            "active classifier profile does not configure model_path",
        )

    try:
        if Path(model_path).is_file():
            return DoctorCheck(
                "model",
                STATUS_OK,
                "active classifier profile model_path is readable",
            )
    except OSError:
        pass
    return DoctorCheck(
        "model",
        STATUS_FAILED,
        "active classifier profile model_path is not a readable file",
    )


def _check_ffmpeg(config: AppConfig) -> DoctorCheck:
    if not bool(config.video.get("enabled", False)):
        return DoctorCheck(
            "ffmpeg",
            STATUS_SKIPPED,
            "video.enabled is false",
        )

    ffmpeg_path = str(config.video.get("ffmpeg_path") or DEFAULT_FFMPEG_PATH)
    if _executable_available(ffmpeg_path):
        return DoctorCheck(
            "ffmpeg",
            STATUS_OK,
            "video.ffmpeg_path is executable or on PATH",
        )
    return DoctorCheck(
        "ffmpeg",
        STATUS_FAILED,
        "video.ffmpeg_path is not executable or not on PATH",
    )


def _probe_directory_writable(directory: Path) -> None:
    created_dirs: list[Path] = []
    target = directory
    while not target.exists():
        created_dirs.append(target)
        parent = target.parent
        if parent == target:
            break
        target = parent

    try:
        if target.exists() and not target.is_dir():
            raise OSError("parent is not a directory")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".mediarefinery-doctor-",
            dir=directory,
            delete=True,
        ):
            pass
    finally:
        for created_dir in created_dirs:
            try:
                created_dir.rmdir()
            except OSError:
                pass


def _immich_config(config: AppConfig) -> dict[str, object]:
    return dict(((config.raw.get("integration") or {}).get("immich") or {}))


def _immich_api_key_env(config: AppConfig) -> str:
    value = _immich_config(config).get("api_key_env")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_API_KEY_ENV


def _doctor_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DOCTOR_NETWORK_TIMEOUT_SECONDS
    if value <= 0:
        return DOCTOR_NETWORK_TIMEOUT_SECONDS
    return min(float(value), DOCTOR_NETWORK_TIMEOUT_SECONDS)


def _immich_api_url(base_url: str, endpoint: str) -> str:
    parsed = urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    api_path = base_path if base_path.endswith("/api") else f"{base_path}/api"
    endpoint_path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"{api_path}{endpoint_path}",
            "",
            "",
            "",
        )
    )


def _capability_message(capabilities: Mapping[str, str]) -> str:
    return (
        "albums="
        f"{capabilities['albums']} tags={capabilities['tags']} "
        f"archive={capabilities['archive']}"
    )


def _find_bool_feature(value: object, names: set[str]) -> bool | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower().replace("-", "_")
            if key_text in names and isinstance(item, bool):
                return item
        for item in value.values():
            found = _find_bool_feature(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_bool_feature(item, names)
            if found is not None:
                return found
    return None


def _executable_available(command: str) -> bool:
    candidate = Path(command)
    if candidate.parent != Path("."):
        return candidate.is_file()
    return shutil.which(command) is not None
