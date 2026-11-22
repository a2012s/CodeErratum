"""Falsification round — a different-vendor model tries to REFUTE each finding.

This is the core mechanism that makes multi-model review more than a vote. The
second model is explicitly instructed to break the first model's conclusion,
because models show confirmation bias when asked to "verify" a presented claim.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from ..models import Finding
from ..providers import Completion, Provider
from .executor import language_directive

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "falsify.md"


class LLMFalsify(BaseModel):
    verdict: str = Field(description="Exactly one of: refuted, confirmed, insufficient_context")
    reason: str = Field(description="Why you reached this verdict; do not restate the claim")
    counterexample: str | None = Field(
        default=None, description="Required when verdict=refuted: concrete input or path"
    )
    broken_assumption: str | None = Field(
        default=None, description="Required when verdict=refuted: the false assumption"
    )

    @model_validator(mode="after")
    def _check(self) -> LLMFalsify:
        if self.verdict not in ("refuted", "confirmed", "insufficient_context"):
            raise ValueError("verdict must be refuted / confirmed / insufficient_context")
        if self.verdict == "refuted" and not (self.counterexample or self.broken_assumption):
            raise ValueError("refuted verdict needs a counterexample or broken_assumption")
        return self


def should_falsify(finding: Finding) -> bool:
    """Cost gate from design §4.4 — falsify only high-value / high-risk findings."""
    if finding.verification == "reproduced":
        return False
    if finding.severity in ("P0", "P1"):
        return True
    if finding.agreement == "unique":
        return True
    # unanimous/majority low-severity: consensus + low risk, not worth the call
    return False


def build_falsify_messages(
    finding: Finding, context: str, known_facts: str, output_lang: str = ""
) -> list[dict[str, str]]:
    anchor = finding.anchors[0]
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        category=finding.category.value,
        claim=finding.claim,
        detail=finding.detail,
        snippet=anchor.snippet.rstrip("\n"),
        context=context.rstrip("\n") or "(no additional context)",
        known_facts=known_facts.rstrip("\n") or "(none)",
    )
    # Language, unlike house rules, travels with the falsifier: a verdict the
    # reader cannot read destroys the independence it exists to provide.
    directive = language_directive(output_lang)
    if directive:
        prompt = f"{directive}\n{prompt}"
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Return your verdict JSON now."},
    ]


async def falsify_finding(
    provider: Provider,
    model_id: str,
    finding: Finding,
    context: str,
    known_facts: str,
    output_lang: str = "",
) -> tuple[LLMFalsify, Completion]:
    """Run one falsification call. Caller decides how to apply the verdict."""
    from ..i18n import get_lang
    from ..models.finding import Verdict

    completion = await provider.complete(
        model_id=model_id,
        messages=build_falsify_messages(finding, context, known_facts, output_lang),
        schema=LLMFalsify,
        temperature=0.0,
    )
    assert isinstance(completion.parsed, LLMFalsify)
    verdict = completion.parsed
    role = {
        "refuted": "refute",
        "confirmed": "confirm",
        "insufficient_context": "abstain",
    }[verdict.verdict]
    # The labels are UI prose, not model output: they follow the run's language,
    # not the falsifier's.
    label_counter, label_assumption = {
        "zh": ("反例", "错误假设"),
        "en": ("Counterexample", "Broken assumption"),
    }.get(output_lang or get_lang(), ("反例", "错误假设"))
    reason = verdict.reason
    if verdict.counterexample:
        reason += f" | {label_counter}: {verdict.counterexample}"
    if verdict.broken_assumption:
        reason += f" | {label_assumption}: {verdict.broken_assumption}"
    finding.verdicts.append(
        Verdict(
            model_id=model_id,
            role=role,  # type: ignore[arg-type]
            reason=reason,
            counterexample=verdict.counterexample,
            spec_level=finding.spec_level,
        )
    )
    return verdict, completion
