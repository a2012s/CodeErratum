"""Finding and its supporting types — the data contract that shapes the product."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["P0", "P1", "P2", "P3"]
SpecLevel = Literal["L0", "L1", "L2", "L3", "L4"]
Agreement = Literal["unanimous", "majority", "unique", "conflict", "unverified"]
Verification = Literal["unverified", "suspected", "reproduced", "accepted"]
FindingStatus = Literal["open", "accepted", "rejected", "promoted_to_rule"]

# collection.rejected mixes two failure shapes that must never be counted
# together: a finding the machine removed from the report, and a unit whose
# model call failed. For unit failures nothing was dropped — with
# ``verify_error`` the finding even survives unverified — so every "machine
# dropped N" metric must exclude these. The kind is a function of the reason,
# which lets old artifacts and the run_rejections table stay unchanged.
UNIT_FAILURE_REASONS: frozenset[str] = frozenset({"provider_error", "verify_error"})


def is_unit_failure(reason: str) -> bool:
    """True when a rejected entry is a failed unit call, not a dropped finding."""
    return reason in UNIT_FAILURE_REASONS


class Lens(StrEnum):
    CORRECTNESS = "correctness"
    CONCURRENCY = "concurrency"
    SECURITY = "security"
    RESILIENCE = "resilience"
    API_CONTRACT = "api_contract"
    DATA = "data"
    TESTABILITY = "testability"
    SPEC_CONFORMANCE = "spec_conformance"


class DefectCategory(StrEnum):
    NULL_DEREF = "null_deref"
    BOUNDARY = "boundary"
    OFF_BY_ONE = "off_by_one"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    RACE_CONDITION = "race_condition"
    DEADLOCK = "deadlock"
    NON_IDEMPOTENT = "non_idempotent"
    MISSING_LOCK = "missing_lock"
    INJECTION = "injection"
    AUTHZ_BYPASS = "authz_bypass"
    SECRET_EXPOSURE = "secret_exposure"
    SSRF = "ssrf"
    PATH_TRAVERSAL = "path_traversal"
    RESOURCE_LEAK = "resource_leak"
    MISSING_TIMEOUT = "missing_timeout"
    RETRY_STORM = "retry_storm"
    MISSING_BACKOFF = "missing_backoff"
    BREAKING_CHANGE = "breaking_change"
    SEMANTIC_DRIFT = "semantic_drift"
    ERROR_CODE_INCONSISTENT = "error_code_inconsistent"
    N_PLUS_ONE = "n_plus_one"
    MISSING_INDEX = "missing_index"
    NON_REVERSIBLE_MIGRATION = "non_reversible_migration"
    TRANSACTION_BOUNDARY = "transaction_boundary"
    MISSING_TEST = "missing_test"
    TEST_CHEAT = "test_cheat"
    HARD_TO_TEST = "hard_to_test"
    SPEC_MISMATCH = "spec_mismatch"
    DEAD_CODE = "dead_code"
    DUPLICATION = "duplication"


def normalize_snippet(text: str) -> str:
    """Normalize a code fragment for stable hashing.

    Strips trailing whitespace per line and leading/trailing blank lines so that
    editor formatting noise does not break anchor verification.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def snippet_hash(text: str) -> str:
    return hashlib.sha256(normalize_snippet(text).encode("utf-8")).hexdigest()[:12]


class Anchor(BaseModel):
    """A verifiable code location. Without a matching hash, a finding is not output."""

    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str | None = None
    snippet: str = Field(description="Verbatim source fragment, used for hash verification")
    snippet_hash: str = Field(description="sha256(normalized(snippet))[:12]")

    @model_validator(mode="after")
    def _check(self) -> Anchor:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        if self.snippet_hash != snippet_hash(self.snippet):
            raise ValueError("snippet_hash mismatch")
        return self

    @classmethod
    def from_source(
        cls,
        file: str,
        start_line: int,
        end_line: int,
        source_lines: list[str],
        symbol: str | None = None,
    ) -> Anchor:
        fragment = "".join(source_lines[start_line - 1 : end_line])
        return cls(
            file=file,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            snippet=fragment,
            snippet_hash=snippet_hash(fragment),
        )

    def verify(self, repo_root: Path) -> bool:
        """Re-read the file and confirm the fragment still matches."""
        try:
            lines = (repo_root / self.file).read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            return False
        if self.end_line > len(lines):
            return False
        current = "".join(lines[self.start_line - 1 : self.end_line])
        return snippet_hash(current) == self.snippet_hash


class Evidence(BaseModel):
    kind: Literal["static_diag", "code_path", "test_failure", "contract_diff", "doc_ref"]
    summary: str
    payload: dict = Field(default_factory=dict)
    strength: Literal["hard", "soft"] = "soft"


class Suggestion(BaseModel):
    """A concrete fix, preferring a directly applicable unified diff."""

    diff: str | None = Field(default=None, description="Unified diff, or None if not applicable")
    explanation: str = Field(description="What to change and why; required when diff is None")
    acceptance: str = Field(description="Command to run and expected result after the change")

    @model_validator(mode="after")
    def _check(self) -> Suggestion:
        if not self.diff and not self.explanation.strip():
            raise ValueError("Suggestion needs either a diff or an explanation")
        if not self.acceptance.strip():
            raise ValueError("Suggestion must have an acceptance command")
        return self


class Verdict(BaseModel):
    model_id: str
    role: Literal["assert", "refute", "confirm", "abstain"]
    reason: str
    counterexample: str | None = None
    spec_level: SpecLevel = "L1"


class Finding(BaseModel):
    """A single executable work order for the downstream coding AI."""

    id: str = Field(description="Stable content hash across runs")
    unit_id: str
    lens: Lens
    category: DefectCategory
    severity: Severity
    claim: str = Field(description="One-sentence conclusion, <= 60 chars")
    detail: str = Field(description="Trigger condition + impact + why it is a problem")
    anchors: list[Anchor] = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    suggestion: Suggestion | None = None
    verdicts: list[Verdict] = Field(default_factory=list)
    source_models: list[str] = Field(
        default_factory=list, description="Models whose first-round review produced this finding"
    )
    agreement: Agreement = "unverified"
    verification: Verification = "unverified"
    spec_level: SpecLevel = "L1"
    dedupe_key: str = ""
    status: FindingStatus = "open"
    first_seen_run: str = ""
    last_seen_run: str = ""

    @model_validator(mode="after")
    def _check(self) -> Finding:
        if not self.claim.strip():
            raise ValueError("claim must not be empty")
        if len(self.claim) > 60:
            raise ValueError("claim must be <= 60 characters")
        if not self.suggestion:
            raise ValueError("Every finding must carry a concrete suggestion")
        return self

    @classmethod
    def content_id(cls, unit_id: str, claim: str, anchor_keys: list[str]) -> str:
        raw = f"{unit_id}::{claim}::{'|'.join(anchor_keys)}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @classmethod
    def make_dedupe_key(cls, anchor: Anchor, category: DefectCategory) -> str:
        bucket = (anchor.start_line - 1) // 5 * 5 + 1
        return f"{anchor.file}:{bucket}-{bucket + 4}:{category.value}"


def is_blocking(finding: Finding) -> bool:
    """Whether a finding is a gate item for a downstream agent or PR check.

    Evidence strength already existed and already influenced *ranking*, but never
    changed disposition. A gate item needs all three of: a high severity, a hard
    static diagnostic (a real tool said so), and an executable acceptance command.
    Severity alone is not enough — a model's P1 hunch must stay advisory — and a
    hard diagnostic without an acceptance command cannot be closed by the reader.
    """
    if finding.severity not in ("P0", "P1"):
        return False
    if not any(ev.kind == "static_diag" and ev.strength == "hard" for ev in finding.evidence):
        return False
    return bool(finding.suggestion and finding.suggestion.acceptance.strip())


class FindingCollection(BaseModel):
    """The full report payload, serialized to runs/<run_id>/findings.json."""

    run_id: str
    repo: str
    commit: str
    model_id: str
    spec_level: SpecLevel
    spec_level_reason: str = Field(
        default="",
        description="Why this level was assigned; shown next to it so the judgement is arguable",
    )
    mode: str = "quick"
    reviewer_models: list[str] = Field(default_factory=list)
    falsifier_model: str | None = None
    comparison: dict = Field(
        default_factory=dict, description="Phase-3 per-model comparison data (for eval/resume)"
    )
    findings: list[Finding] = Field(default_factory=list)
    # A single-source claim broken by the falsifier is not a surviving finding,
    # but it is a precision sample for the model that asserted it. Keeping the
    # full object here (instead of only a row in `rejected`) lets the profile
    # writer accumulate n_refuted without ever writing it to `findings`, whose
    # status column means a human verdict.
    refuted_findings: list[Finding] = Field(
        default_factory=list, description="Surviving-worthy claims the falsifier refuted"
    )
    rejected: list[dict] = Field(
        default_factory=list,
        description=(
            "Machine-side removals and failures: per-finding drops "
            "(anchor_mismatch, duplicate, invalid_finding, refuted) and "
            "unit-level call failures (provider_error, verify_error). "
            "See is_unit_failure() before counting these as dropped findings."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Visible degradations for this run (skipped files, failed units, ...)",
    )
    stopped: bool = Field(
        default=False,
        description="True when the user stopped the run; findings cover only completed units "
        "and were not fully cross-checked.",
    )
    house_rules_sha: str = Field(
        default="",
        description="sha256 of the appended project rules; empty when none were applied. "
        "Two runs over the same diff can differ only because of this, so it must be traceable.",
    )
    house_rules_chars: int = Field(default=0, description="Length of the applied project rules")
    house_rules_source: str = Field(
        default="", description="Where the rules came from: 'flag' | file name | ''"
    )

    def sorted_findings(self) -> list[Finding]:
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(self.findings, key=lambda f: (order[f.severity], f.id))

    def ranked_findings(self) -> list[Finding]:
        """Order for display: severity, then hard evidence, then agreement signal.

        Sorting by (severity, id) alone threw away the aggregator's ranking — the
        id is a content hash, so evidence-backed findings landed at random.
        """
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        agreement_order = {
            "conflict": 0,
            "unanimous": 1,
            "unique": 2,
            "majority": 3,
            "unverified": 4,
        }

        def key(f: Finding) -> tuple:
            hard = 0 if any(e.strength == "hard" for e in f.evidence) else 1
            return (order[f.severity], hard, agreement_order.get(f.agreement, 5), f.id)

        return sorted(self.findings, key=key)

    def zoned(self) -> dict[str, list[Finding]]:
        """Split into the three report zones. Quick mode yields only 'all'."""
        if self.mode == "quick":
            return {"all": self.ranked_findings()}
        zones: dict[str, list[Finding]] = {"consensus": [], "unique": [], "conflict": []}
        for f in self.ranked_findings():
            if f.agreement == "conflict":
                zones["conflict"].append(f)
            elif f.agreement in ("unanimous", "majority"):
                zones["consensus"].append(f)
            else:  # unique / unverified / refuted-away survivors
                zones["unique"].append(f)
        return zones
