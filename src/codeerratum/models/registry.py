"""Model registry — heterogeneous model selection and routing.

Heterogeneity is judged by ``family`` (vendor), not by model version. Two models
from the same vendor with different snapshots do NOT count as heterogeneous —
they share correlated blind spots, which is exactly what phase 3 exists to break.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Mode = Literal["quick", "dual", "committee"]

# Known vendor families, keyed by the leading token of model_id. Unknown models
# fall back to their full id as the family, so distinct unknown models are still
# treated as heterogeneous (safe default for the heterogeneity constraint).
_FAMILY_PREFIXES: dict[str, str] = {
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "claude": "anthropic",
    "gemini": "google",
    "deepseek": "deepseek",
    "qwen": "alibaba",
    "glm": "zhipu",
    "kimi": "moonshot",
    "ernie": "baidu",
    "llama": "meta",
    "mistral": "mistral",
}


def derive_family(model_id: str) -> str:
    """Infer vendor family from a model id (e.g. 'gpt-4o' -> 'openai')."""
    head = model_id.strip().lower().split("-")[0].split("/")[-1]
    for prefix, family in _FAMILY_PREFIXES.items():
        if head.startswith(prefix):
            return family
    return model_id.strip().lower()


class ModelSpec(BaseModel):
    """One review model endpoint. Multiple specs with distinct families enable phase 3."""

    model_id: str = Field(description="Model id, pinned to a snapshot — never 'latest'")
    base_url: str
    api_key: str = ""
    family: str = Field(default="", description="Vendor family; derived from model_id if empty")
    affinity: dict[str, float] = Field(default_factory=dict)
    measured_n: int = 0
    measured_precision: float | None = None
    measured_unique_rate: float | None = None

    @field_validator("family", mode="before")
    @classmethod
    def _derive(cls, value: str) -> str:
        return value or ""

    def resolved_family(self) -> str:
        return self.family or derive_family(self.model_id)

    def score(self, lenses: list[str]) -> float:
        """Prior affinity for the given lenses; measured precision only counts at n>=30."""
        base = sum(self.affinity.get(lens, 0.5) for lens in lenses) / max(len(lenses), 1)
        if self.measured_n >= 30 and self.measured_precision is not None:
            # Measured data overrides prior, weighted by sample confidence.
            confidence = min(self.measured_n / 100.0, 1.0)
            base = base * (1 - confidence) + self.measured_precision * confidence
        return base


class ModelRegistry(BaseModel):
    models: list[ModelSpec]

    @field_validator("models")
    @classmethod
    def _unique_ids(cls, models: list[ModelSpec]) -> list[ModelSpec]:
        ids = [m.model_id for m in models]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate model_id in registry")
        return models

    @classmethod
    def from_json(cls, raw: str) -> ModelRegistry:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("models", [])
        return cls.model_validate({"models": data})

    def available(self, exclude_author: str | None = None) -> list[ModelSpec]:
        """Models eligible for a review seat — never the code's author model."""
        return [m for m in self.models if m.model_id != exclude_author]

    def route(
        self,
        mode: Mode,
        author_model: str | None = None,
        lenses: list[str] | None = None,
    ) -> tuple[list[ModelSpec], ModelSpec | None]:
        """Pick reviewers and an optional falsifier.

        Returns (reviewers, falsifier). In dual/committee mode reviewers have
        distinct families. The falsifier is drawn from a family not used by any
        reviewer (a third vendor when available), else None — callers then fall
        back to cross-falsification between reviewers.
        """
        lenses = lenses or []
        pool = self.available(author_model)
        if not pool:
            raise ValueError("no eligible review model (all excluded as author?)")

        n = {"quick": 1, "dual": 2, "committee": 3}[mode]
        ranked = sorted(pool, key=lambda m: -m.score(lenses))

        reviewers: list[ModelSpec] = []
        seen_families: set[str] = set()
        for m in ranked:
            fam = m.resolved_family()
            if n >= 2 and fam in seen_families:
                continue
            reviewers.append(m)
            seen_families.add(fam)
            if len(reviewers) == n:
                break

        if n >= 2 and len(reviewers) < n:
            raise ValueError(
                f"mode '{mode}' needs {n} heterogeneous models but only "
                f"{len(reviewers)} distinct families are available"
            )

        falsifier: ModelSpec | None = None
        if n >= 2:
            for m in ranked:
                if m.resolved_family() not in seen_families:
                    falsifier = m
                    break
        return reviewers, falsifier

    @staticmethod
    def heterogenous(a: ModelSpec, b: ModelSpec) -> bool:
        return a.resolved_family() != b.resolved_family()
