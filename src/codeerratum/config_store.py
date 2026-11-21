"""Persisted configuration for the local UI (``.codeerratum/config.json``).

Environment variables stay the CLI default and the bootstrap source. Once a
config file exists it wins, so an edit made in the browser survives a restart
without re-exporting anything. The file holds API keys, hence the 0600 mode and
the masked read path — ``public()`` is the only shape allowed to reach HTTP.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import Config
from .models import ModelRegistry, ModelSpec

CONFIG_REL = Path(".codeerratum") / "config.json"
KEEP_SECRET = "__KEEP__"  # client sends this to retain an existing api_key
MODES = ("quick", "dual", "committee")
MAX_MODELS = 20
_URL_RE = re.compile(r"^https?://", re.I)


def config_path(repo_root: Path) -> Path:
    return repo_root.resolve() / CONFIG_REL


def load(repo_root: Path) -> Config | None:
    """Read the config file; return None when absent (env remains the source).

    A malformed file is an error, never a silent fallback to env: falling back
    would make a broken edit look like the old config still worked.
    """
    path = config_path(repo_root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{CONFIG_REL} must be a JSON object")
    mode = str(data.get("mode") or "quick").strip().lower()
    if mode not in MODES:
        raise ValueError(f"{CONFIG_REL}: invalid mode {mode!r}; choose quick/dual/committee")
    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError(f"{CONFIG_REL}: models must be a non-empty array")
    registry = ModelRegistry.model_validate({"models": models})
    primary = registry.models[0]
    return Config(
        base_url=primary.base_url,
        api_key=primary.api_key,
        model=primary.model_id,
        registry=registry,
        mode=mode,  # type: ignore[arg-type]
    )


def effective(repo_root: Path) -> tuple[Config | None, str]:
    """Return (config, source). Used by both the UI read paths and the pipeline."""
    try:
        file_config = load(repo_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {CONFIG_REL}: {exc}") from exc
    if file_config is not None:
        return file_config, "file"
    try:
        return Config.from_env(), "env"
    except ValueError:
        return None, "none"


def mask_secret(value: str) -> str:
    """Never send a full key to the browser, not even over loopback."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}***{value[-4:]}"


def public(repo_root: Path) -> dict:
    """Masked description of the active configuration, safe for HTTP."""
    info: dict = {
        "path": str(CONFIG_REL),
        "source": "none",
        "configured": False,
        "mode": "quick",
        "models": [],
        "config_error": "",
    }
    try:
        config, source = effective(repo_root)
    except ValueError as exc:
        info["config_error"] = str(exc)
        return info
    if config is None:
        info["config_error"] = "未配置模型：设置环境变量，或在界面里保存配置到 " + str(CONFIG_REL)
        return info
    info.update(
        configured=True,
        source=source,
        mode=config.mode,
        models=[
            {
                "model_id": m.model_id,
                "base_url": m.base_url,
                "family": m.family,
                "resolved_family": m.resolved_family(),
                "affinity": m.affinity,
                "api_key_set": bool(m.api_key),
                "api_key_hint": mask_secret(m.api_key),
            }
            for m in config.registry.models
        ],
    )
    return info


def save(repo_root: Path, payload: dict) -> dict:
    """Validate and persist a config posted by the UI; return ``public()``."""
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    models = payload.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise ValueError(f"models must be an array of 1..{MAX_MODELS} entries")

    previous: dict[str, ModelSpec] = {}
    try:
        old = load(repo_root)
    except (OSError, ValueError):
        old = None
    if old:
        previous = {m.model_id: m for m in old.registry.models}

    specs: list[ModelSpec] = []
    for raw in models:
        if not isinstance(raw, dict):
            raise ValueError("each model must be an object")
        model_id = str(raw.get("model_id") or "").strip()
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        family = str(raw.get("family") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        if not model_id or not base_url:
            raise ValueError("model_id and base_url are required")
        if len(model_id) > 200 or len(base_url) > 500:
            raise ValueError("model_id or base_url is too long")
        if not _URL_RE.match(base_url):
            raise ValueError(f"base_url must start with http:// or https:// ({model_id})")
        if api_key == KEEP_SECRET:
            api_key = previous.get(model_id).api_key if model_id in previous else ""
        if len(api_key) > 2000:
            raise ValueError(f"api_key is too long ({model_id})")
        affinity = raw.get("affinity")
        if affinity is not None and not isinstance(affinity, dict):
            raise ValueError(f"affinity must be an object ({model_id})")
        measured = previous.get(model_id)
        specs.append(
            ModelSpec(
                model_id=model_id,
                base_url=base_url,
                api_key=api_key,
                family=family,
                affinity={str(k): float(v) for k, v in (affinity or {}).items()},
                measured_n=measured.measured_n if measured else 0,
                measured_precision=measured.measured_precision if measured else None,
                measured_unique_rate=measured.measured_unique_rate if measured else None,
            )
        )

    # Let the registry reject duplicate ids before touching disk.
    registry = ModelRegistry(models=specs)
    payload_out = {
        "mode": mode,
        "models": [m.model_dump(mode="json") for m in registry.models],
    }
    path = config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload_out, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return public(repo_root)
