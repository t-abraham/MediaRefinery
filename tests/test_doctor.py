from __future__ import annotations

from pathlib import Path
from typing import Sequence
import copy

import pytest
import yaml

from mediarefinery.config import AppConfig
from mediarefinery import doctor
from mediarefinery.doctor import (
    DoctorCheck,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WARNING,
    run_doctor_checks,
)


def _example_data(tmp_path: Path) -> dict:
    data = copy.deepcopy(yaml.safe_load(Path("templates/config.example.yml").read_text()))
    data["integration"]["immich"]["url"] = "http://127.0.0.1:9"
    data["state"]["sqlite_path"] = str(tmp_path / "state.sqlite3")
    data["runtime"]["temp_dir"] = str(tmp_path / "tmp")
    return data


def _write_config(tmp_path: Path, data: dict) -> Path:
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config_path


def _ok_immich_probe(
    config: AppConfig,
    api_key: str,
) -> Sequence[DoctorCheck]:
    return (
        DoctorCheck("immich", STATUS_OK, "configured Immich server responded"),
        DoctorCheck(
            "immich auth",
            STATUS_OK,
            "API key env var was accepted by a read-only Immich endpoint",
        ),
        DoctorCheck(
            "immich capabilities",
            STATUS_OK,
            "albums=assumed tags=available archive=unavailable",
        ),
    )


def test_doctor_reports_missing_api_key_env_by_name(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, _example_data(tmp_path))

    result = run_doctor_checks(
        config_path,
        environ={},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 1
    environment = _check(result.checks, "environment")
    assert environment.status == STATUS_FAILED
    assert "IMMICH_API_KEY" in environment.message
    assert "replace_me" not in environment.message
    assert _check(result.checks, "immich").status == STATUS_SKIPPED


def test_doctor_uses_api_key_value_without_reporting_it(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, _example_data(tmp_path))
    seen_api_keys: list[str] = []

    def probe(config: AppConfig, api_key: str) -> Sequence[DoctorCheck]:
        seen_api_keys.append(api_key)
        return _ok_immich_probe(config, api_key)

    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "super-secret-test-key"},
        immich_probe=probe,
    )

    assert result.exit_code == 0
    assert seen_api_keys == ["super-secret-test-key"]
    rendered_lines: list[str] = []
    for check in result.checks:
        rendered_lines.append(check.message)
        rendered_lines.extend(check.details)
    rendered = "\n".join(rendered_lines)
    assert "super-secret-test-key" not in rendered


def test_doctor_network_warnings_do_not_fail_offline_readiness(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, _example_data(tmp_path))

    def offline_probe(
        config: AppConfig,
        api_key: str,
    ) -> Sequence[DoctorCheck]:
        return (
            DoctorCheck(
                "immich",
                STATUS_WARNING,
                "configured Immich server was not reachable; offline local checks completed",
            ),
        )

    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=offline_probe,
    )

    assert result.exit_code == 0
    assert _check(result.checks, "immich").status == STATUS_WARNING


@pytest.mark.parametrize("path_key", ["state", "temp"])
def test_doctor_reports_unwritable_or_invalid_paths(
    tmp_path: Path,
    path_key: str,
) -> None:
    data = _example_data(tmp_path)
    blocking_file = tmp_path / "blocking-file"
    blocking_file.write_text("not a directory", encoding="utf-8")
    if path_key == "state":
        data["state"]["sqlite_path"] = str(blocking_file / "state.sqlite3")
    else:
        data["runtime"]["temp_dir"] = str(blocking_file)
    config_path = _write_config(tmp_path, data)

    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 3
    assert _check(result.checks, path_key).status == STATUS_FAILED


def test_doctor_checks_model_path_only_when_configured(tmp_path: Path) -> None:
    data = _example_data(tmp_path)
    config_path = _write_config(tmp_path, data)

    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 0
    assert _check(result.checks, "model").status == STATUS_SKIPPED

    data["classifier_profiles"]["default"]["model_path"] = str(tmp_path / "missing.onnx")
    config_path = _write_config(tmp_path, data)
    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 1
    model_check = _check(result.checks, "model")
    assert model_check.status == STATUS_FAILED
    assert "missing.onnx" not in model_check.message

    model_file = tmp_path / "model.onnx"
    model_file.write_text("synthetic model placeholder", encoding="utf-8")
    data["classifier_profiles"]["default"]["model_path"] = str(model_file)
    config_path = _write_config(tmp_path, data)
    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 0
    assert _check(result.checks, "model").status == STATUS_OK


def test_doctor_checks_ffmpeg_only_when_video_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _example_data(tmp_path)
    config_path = _write_config(tmp_path, data)

    monkeypatch.setattr(
        doctor,
        "_executable_available",
        lambda command: pytest.fail("ffmpeg should not be checked"),
    )
    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 0
    assert _check(result.checks, "ffmpeg").status == STATUS_SKIPPED

    data["scanner"]["media_types"] = ["video"]
    data["video"]["enabled"] = True
    config_path = _write_config(tmp_path, data)
    monkeypatch.setattr(doctor, "_executable_available", lambda command: False)
    result = run_doctor_checks(
        config_path,
        environ={"IMMICH_API_KEY": "placeholder"},
        immich_probe=_ok_immich_probe,
    )

    assert result.exit_code == 1
    assert _check(result.checks, "ffmpeg").status == STATUS_FAILED


def _check(checks: Sequence[DoctorCheck], name: str) -> DoctorCheck:
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"missing doctor check {name}")
