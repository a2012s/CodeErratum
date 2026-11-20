"""Configuration — environment variables only, no config file.

Backward compatible: the original three variables still define the single
(single-model / quick) reviewer. Phase 3 adds:

  CODEERRATUM_MODELS  JSON array of {model_id, base_url, api_key?, family?, ...}
  CODEERRATUM_MODE    quick | dual | committee   (default: quick)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .models.registry import Mode, ModelRegistry, ModelSpec


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    registry: ModelRegistry
    mode: Mode = "quick"

    @classmethod
    def from_env(cls) -> Config:
        base_url = os.environ.get("CODEERRATUM_BASE_URL", "").strip()
        api_key = os.environ.get("CODEERRATUM_API_KEY", "").strip()
        model = os.environ.get("CODEERRATUM_MODEL", "").strip()
        models_raw = os.environ.get("CODEERRATUM_MODELS", "").strip()
        mode = os.environ.get("CODEERRATUM_MODE", "quick").strip().lower()

        if mode not in ("quick", "dual", "committee"):
            raise ValueError(
                f"Invalid CODEERRATUM_MODE={mode!r}; choose from quick, dual, committee"
            )

        if models_raw:
            try:
                registry = ModelRegistry.from_json(models_raw)
            except Exception as exc:  # noqa: BLE001 - surface a clear config error
                raise ValueError(f"CODEERRATUM_MODELS is not valid JSON: {exc}") from exc
            if not registry.models:
                raise ValueError("CODEERRATUM_MODELS must contain at least one model")
            primary = registry.models[0]
            return cls(
                base_url=primary.base_url,
                api_key=primary.api_key,
                model=primary.model_id,
                registry=registry,
                mode=mode,  # type: ignore[arg-type]
            )

        # Legacy single-model path.
        missing = [
            name
            for name, val in (("CODEERRATUM_BASE_URL", base_url), ("CODEERRATUM_MODEL", model))
            if not val
        ]
        if missing:
            raise ValueError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + "\nExample: export CODEERRATUM_BASE_URL=https://api.example.com/v1"
                + "\n         export CODEERRATUM_API_KEY=sk-... (optional for local endpoints)"
                + "\n         export CODEERRATUM_MODEL=your-model-snapshot"
                + "\nFor multi-model review, set CODEERRATUM_MODELS instead."
            )
        registry = ModelRegistry(
            models=[ModelSpec(model_id=model, base_url=base_url, api_key=api_key)]
        )
        return cls(base_url=base_url, api_key=api_key, model=model, registry=registry, mode=mode)  # type: ignore[arg-type]
