"""Model registry — heterogeneous model selection and routing.

Heterogeneity is judged by ``model_id`` (a pinned snapshot), not by vendor. Two
versions from the same vendor DO count as distinct reviewers: different versions
have different emphases and can catch each other's blind spots. ``family`` is
kept as a display/report label only; it never gates reviewer selection.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Mode = Literal["quick", "dual", "committee"]

# Known vendor families, keyed by the leading token of model_id. Used only as a
# display/report label — reviewer selection keys on model_id, so two versions of
# the same vendor are both eligible. Unknown models fall back to their full id.
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
    """One review model endpoint. Distinct model_ids enable multi-model review."""

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

    def _check_author(self, author_model: str | None) -> None:
        """Reject an author id that is not configured; a typo is not a valid ban.

        ``available(author)`` silently excludes nothing when the id is unknown,
        which would let the real author model take a reviewer seat. Failing here
        makes the mistake visible instead of deferring it to the report.
        """
        if author_model and author_model not in {m.model_id for m in self.models}:
            raise ValueError(f"unknown author model id: {author_model}")

    def route(
        self,
        mode: Mode,
        author_model: str | None = None,
        lenses: list[str] | None = None,
        pinned: list[str] | None = None,
    ) -> tuple[list[ModelSpec], ModelSpec | None]:
        """Pick reviewers and an optional falsifier.

        Returns (reviewers, falsifier). Reviewer identity is the pinned
        ``model_id``: two versions of the same vendor are two reviewers. The
        falsifier is a model not used as a reviewer (a spare model when one is
        available), else None — callers then fall back to cross-falsification
        between the reviewers themselves.

        ``pinned`` names the seats explicitly, in order. The user picked those
        models because they know which one writes the reviews they trust — the
        scorer is only a default, never an override.
        """
        lenses = lenses or []
        n = {"quick": 1, "dual": 2, "committee": 3}[mode]
        self._check_author(author_model)
        if pinned:
            by_id = {m.model_id: m for m in self.models}
            unknown = [mid for mid in pinned if mid not in by_id]
            if unknown:
                raise ValueError(f"unknown model id(s): {', '.join(unknown)}")
            # Explicit pins bypass the scoring pool, but never the author ban.
            # Without this filter a user who pins the author model gets it seated
            # as its own reviewer — the one promise this product cannot break.
            eligible = [mid for mid in dict.fromkeys(pinned) if mid != author_model]
            if not eligible:
                raise ValueError("all pinned model(s) are excluded as the author model")
            # Dedupe while keeping the user's order, and never seat one model twice.
            reviewers = [by_id[mid] for mid in eligible]
            if len(reviewers) != n:
                raise ValueError(
                    f"mode '{mode}' takes {n} model(s) but {len(reviewers)} were selected"
                )
            taken = {m.model_id for m in reviewers}
            # The author ban covers every seat, not just reviewers. Selecting
            # the falsifier from self.models would let the author judge its own
            # code from the other side of the table.
            falsifier = (
                next(
                    (m for m in self.available(author_model) if m.model_id not in taken),
                    None,
                )
                if n >= 2
                else None
            )
            return reviewers, falsifier

        pool = self.available(author_model)
        if not pool:
            raise ValueError("no eligible review model (all excluded as author?)")

        ranked = sorted(pool, key=lambda m: -m.score(lenses))

        reviewers = ranked[:n]
        if len(reviewers) < n:
            raise ValueError(
                f"mode '{mode}' needs {n} heterogeneous models but only "
                f"{len(reviewers)} distinct models are available"
            )

        reviewer_ids = {m.model_id for m in reviewers}
        falsifier: ModelSpec | None = None
        if n >= 2:
            falsifier = next((m for m in ranked if m.model_id not in reviewer_ids), None)
        return reviewers, falsifier

    @staticmethod
    def heterogenous(a: ModelSpec, b: ModelSpec) -> bool:
        """Two specs are heterogeneous when they are distinct pinned snapshots."""
        return a.model_id != b.model_id

    def route_quick_tier(
        self,
        pinned: list[str] | None = None,
        author_model: str | None = None,
    ) -> tuple[ModelSpec, ModelSpec | None]:
        """Pick (fast scanner, strong verifier) for a tiered quick review.

        A pinned model is the one the user trusts with the verdict, so it takes
        the verifier seat; the scanner is the lightest *other* configured model.
        With only one model available the verifier is None and the caller falls
        back to an ordinary single-model quick run — never a second call to the
        same model sold as independent confirmation.
        """
        self._check_author(author_model)
        pool = self.available(author_model)
        if not pool:
            raise ValueError("no eligible review model (all excluded as author?)")
        by_id = {m.model_id: m for m in self.models}
        unknown = [mid for mid in (pinned or []) if mid not in by_id]
        if unknown:
            raise ValueError(f"unknown model id(s): {', '.join(unknown)}")
        trusted = [by_id[mid] for mid in dict.fromkeys(pinned or []) if by_id[mid] in pool]

        def lateness(m: ModelSpec) -> tuple[int, float, str]:
            # Model ids normally carry the speed class (flash/mini/lite/...).
            # This is a routing hint, never a claim about benchmarked latency.
            light = any(
                word in m.model_id.lower()
                for word in ("flash", "mini", "lite", "turbo", "nano", "haiku", "fast", "8b", "7b")
            )
            return (0 if light else 1, -m.score([]), m.model_id)

        ranked_strong = sorted(pool, key=lambda m: (-m.score([]), m.model_id))
        verifier = trusted[0] if trusted else ranked_strong[0]
        others = [m for m in pool if m.model_id != verifier.model_id]
        if not others:
            return verifier, None
        return sorted(others, key=lateness)[0], verifier
