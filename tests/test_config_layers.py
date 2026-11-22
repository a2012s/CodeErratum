"""Config resolution: project file → user default → env.

The bug these lock down: adding a second project showed an empty settings
drawer and asked for the same endpoints and keys again, because the roster was
stored only inside one repository's ``.codeerratum/config.json``.
"""

from __future__ import annotations

import json
import stat

import pytest

from codeerratum import config_store


def roster(model_id="deepseek-v4.1", key="sk-test-key", mode="quick"):
    return {
        "mode": mode,
        "models": [
            {
                "model_id": model_id,
                "base_url": "https://api.example.com/v1",
                "family": "deepseek",
                "api_key": key,
            }
        ],
    }


def test_a_new_project_inherits_the_default_roster(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    config_store.save(first, roster())

    config, source = config_store.effective(second)
    assert source == "user", "no project file yet, so the default must answer"
    assert config is not None
    assert [m.model_id for m in config.registry.models] == ["deepseek-v4.1"]
    assert config.registry.models[0].api_key == "sk-test-key"
    assert config.mode == "quick"


def test_the_drawer_names_the_default_file_not_a_phantom_project_file(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster())

    info = config_store.public(second)
    assert info["configured"] is True
    assert info["source"] == "user"
    assert info["path"] == config_store.USER_CONFIG_LABEL
    assert info["default_path"] == config_store.USER_CONFIG_LABEL
    assert info["models"][0]["api_key_hint"].endswith("key")


def test_a_project_file_still_wins_over_the_default(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster())
    config_store.save(second, roster(model_id="qwen3.8-flash", key="sk-other"))

    config, source = config_store.effective(second)
    assert source == "file"
    assert [m.model_id for m in config.registry.models] == ["qwen3.8-flash"]
    # And saving the second project did not clobber the first one's own roster.
    config_store.save(first, roster())
    assert [m.model_id for m in config_store.load(first).registry.models] == ["deepseek-v4.1"]


def test_saving_without_the_checkbox_leaves_the_default_alone(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster(model_id="deepseek-v4.1"))
    before = config_store.user_config_path().read_text(encoding="utf-8")

    config_store.save(second, roster(model_id="qwen3.8-flash") | {"save_as_default": False})

    assert config_store.user_config_path().read_text(encoding="utf-8") == before
    # The project still got its own file, so it is not left on the default.
    assert config_store.public(second)["source"] == "file"


def test_keep_secret_resolves_against_the_default_layer(tmp_path):
    """A project reading the default shows a masked key; saving sends __KEEP__."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster(key="sk-real-secret"))

    keeps = roster(key=config_store.KEEP_SECRET) | {"save_as_default": False}
    config_store.save(second, keeps)

    saved = config_store.load(second)
    assert saved.registry.models[0].api_key == "sk-real-secret"


def test_a_broken_default_is_an_error_not_a_silent_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://env.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-env")
    home = config_store.user_home()
    home.mkdir(parents=True, exist_ok=True)
    config_store.user_config_path().write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="config.json"):
        config_store.effective(tmp_path)


def test_the_default_file_is_private(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_store.save(repo, roster())

    path = config_store.user_config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["models"][0]["api_key"] == "sk-test-key"


def test_env_still_answers_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://env.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-env")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    config, source = config_store.effective(tmp_path)
    assert source == "env"
    assert [m.model_id for m in config.registry.models] == ["gpt-env"]


def test_the_model_picker_offers_the_inherited_roster(tmp_path):
    """available_models() must match what a run would use, default included."""
    from codeerratum.pipeline import available_models

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster())

    assert [m["id"] for m in available_models(second)] == ["deepseek-v4.1"]


def test_web_meta_reports_the_inherited_roster(tmp_path):
    from codeerratum.web.server import WebApp

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_store.save(first, roster())

    meta = WebApp(second).meta()
    assert meta["configured"] is True
    assert meta["source"] == "user"
    assert [m["model_id"] for m in meta["models"]] == ["deepseek-v4.1"]
