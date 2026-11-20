"""ReviewUnit — the atomic unit of review: one modified symbol, not one file."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, model_validator

UnitKind = Literal["function", "class", "module", "config"]


class Hunk(BaseModel):
    """A contiguous changed block from a unified diff."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    content: str = Field(description="Raw hunk body including context and +/- lines")

    @property
    def new_range(self) -> tuple[int, int]:
        if self.new_lines == 0:
            return (self.new_start, self.new_start)
        return (self.new_start, self.new_start + self.new_lines - 1)


class ReviewUnit(BaseModel):
    """One modified symbol plus its diff hunks.

    Splitting by symbol (not by file) keeps each review prompt focused and lets
    the planner attach the right lens later.
    """

    unit_id: str = Field(description="sha256(file + symbol + hunk_ranges)[:16]")
    primary_file: str = Field(description="Path relative to repo root")
    symbol: str = Field(description="Qualified symbol name, or '<module>' / '<config>'")
    kind: UnitKind
    language: str = "python"
    hunks: list[Hunk]
    changed_lines: int
    is_public: bool = False
    risk_tags: list[str] = Field(default_factory=list)
    author_model: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ReviewUnit:
        if not self.primary_file:
            raise ValueError("primary_file must not be empty")
        if not self.hunks:
            raise ValueError("ReviewUnit must contain at least one hunk")
        if self.changed_lines < 0:
            raise ValueError("changed_lines must be >= 0")
        return self

    @classmethod
    def make_id(cls, file: str, symbol: str, hunks: list[Hunk]) -> str:
        ranges = "|".join(f"{h.new_start}-{h.new_start + h.new_lines}" for h in hunks)
        raw = f"{file}::{symbol}::{ranges}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @property
    def changed_lines_summary(self) -> str:
        return "+".join(str(h.new_lines) for h in self.hunks)
