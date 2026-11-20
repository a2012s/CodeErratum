"""Structured output schema for the LLM — strict, closed enums, no free-form fields."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models.finding import DefectCategory, Lens, Severity


class LLMSuggestion(BaseModel):
    diff: str | None = Field(
        default=None,
        description="A unified diff that can be applied directly. Prefer this over prose.",
    )
    explanation: str = Field(description="What to change and why. Required if diff is null.")
    acceptance: str = Field(
        description="Exact command to run after the change and the expected result."
    )


class LLMAnchor(BaseModel):
    file: str = Field(description="Path relative to repo root, copied verbatim from the input")
    start_line: int = Field(description="First line number of the verbatim snippet")
    end_line: int = Field(description="Last line number of the verbatim snippet")
    snippet: str = Field(
        description="EXACT verbatim source lines from the provided code. Copy characters literally."
    )


class LLMFinding(BaseModel):
    lens: Lens = Field(description="The review perspective that found this")
    category: DefectCategory = Field(description="Closed taxonomy — pick the closest one")
    severity: Severity = Field(
        description="P0=blocks release, P1=should fix, P2=nice to fix, P3=minor"
    )
    claim: str = Field(description="One-sentence conclusion, at most 60 characters")
    detail: str = Field(description="Trigger condition + impact + why this is a problem")
    anchor: LLMAnchor
    suggestion: LLMSuggestion


class LLMReview(BaseModel):
    findings: list[LLMFinding] = Field(
        description="List of concrete issues. Empty list if no real problems found."
    )
    summary: str = Field(description="One-sentence overall assessment of the change")
