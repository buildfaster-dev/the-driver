"""Deterministic scan-vs-review confrontation gate.

detect_discrepancies() is pure and rule-based: it compares ScanResult against
ReviewResult using ONLY fields of those dataclasses — no LLM involved, and no
references to languages, frameworks, or filenames of the analyzed repo
(language-agnosticism product rule). "The scanner does not cover this
ecosystem" is a resolution the reviewer may give, never a rule the gate
hardcodes.

resolve_discrepancies() adds one bounded model call so each detected
contradiction reaches the report with an explicit resolution instead of two
verdicts printed side by side.
"""

import json

import click
from vetter import router
from vetter.models import Discrepancy, ReviewResult, ScanResult

# Rule thresholds, named and deliberate.
STRONG_SCORE = 4  # rubric: 4 already means "good/comprehensive"
SECURITY_FLAGS_THRESHOLD = 3  # fewer isolated flags are likely scanner noise

RESOLUTION_SYSTEM_PROMPT = """You are the same Staff Software Engineer who just reviewed a candidate's technical test submission. An automated gate compared your review against the static scanner's findings and detected contradictions.

For each contradiction, state which verdict should be trusted and why, in 1-3 specific sentences. Legitimate resolutions include "the scanner does not cover this ecosystem's conventions" or "my review under-weighted the scanner's signal — the flag stands". Do not split the difference or answer generically.

Respond ONLY with valid JSON: one object mapping each rule id to its resolution string."""


def _pillar_texts(review: ReviewResult) -> dict[str, str]:
    return {
        pillar.name: " ".join([pillar.justification, *pillar.evidence])
        for pillar in (
            review.architecture_awareness,
            review.code_refinement,
            review.edge_case_coverage,
        )
    }


def detect_discrepancies(scan: ScanResult, review: ReviewResult) -> list[Discrepancy]:
    """Pure rule evaluation over dataclass fields. No I/O, no model."""
    discrepancies: list[Discrepancy] = []

    if scan.error_handling == "minimal" and review.edge_case_coverage.score >= STRONG_SCORE:
        discrepancies.append(Discrepancy(
            rule="error-handling-conflict",
            scan_says="error handling pattern MINIMAL — critical paths may be unprotected",
            review_says=(
                f"Edge Case Coverage {review.edge_case_coverage.score}/5: "
                f"{review.edge_case_coverage.justification}"
            ),
        ))

    lint_mentions = [
        f"{name}: {text}"
        for name, text in _pillar_texts(review).items()
        if "lint" in text.lower()
    ]
    if not scan.has_linter_config and lint_mentions:
        discrepancies.append(Discrepancy(
            rule="linter-conflict",
            scan_says="no linter/formatter configuration detected",
            review_says="the review discusses linting — " + " | ".join(lint_mentions),
        ))

    if not scan.dependencies and review.code_refinement.score >= STRONG_SCORE:
        discrepancies.append(Discrepancy(
            rule="dependencies-conflict",
            scan_says="no dependency manifest detected",
            review_says=(
                f"Code Refinement {review.code_refinement.score}/5 (a pillar that "
                f"includes library choices): {review.code_refinement.justification}"
            ),
        ))

    if (
        len(scan.security_flags) >= SECURITY_FLAGS_THRESHOLD
        and review.edge_case_coverage.score >= STRONG_SCORE
    ):
        discrepancies.append(Discrepancy(
            rule="security-flags-unaddressed",
            scan_says=(
                f"{len(scan.security_flags)} potential hardcoded secrets flagged: "
                + "; ".join(scan.security_flags[:5])
                + ("; ..." if len(scan.security_flags) > 5 else "")
            ),
            review_says=(
                f"Edge Case Coverage {review.edge_case_coverage.score}/5 (a pillar that "
                f"includes security considerations): {review.edge_case_coverage.justification}"
            ),
        ))

    return discrepancies


def _resolution_user_content(discrepancies: list[Discrepancy]) -> str:
    lines = ["The gate detected these scanner-vs-review contradictions:", ""]
    for d in discrepancies:
        lines.append(f"- rule: {d.rule}")
        lines.append(f"  scanner: {d.scan_says}")
        lines.append(f"  review: {d.review_says}")
    lines.append("")
    lines.append('Return JSON mapping every rule id above to its resolution: {"<rule>": "<resolution>"}')
    return "\n".join(lines)


def resolve_discrepancies(discrepancies: list[Discrepancy], model: str = "sonnet") -> list[Discrepancy]:
    """Ask the reviewer to resolve each contradiction explicitly.

    Bounded confrontation by design: the user content is ONLY the detected
    discrepancies (scan fields + the review justifications involved) — the
    repo context/file contents are never re-sent. Hundreds of tokens, not
    thousands.
    """
    if not discrepancies:
        return discrepancies

    response = router.complete(
        system=RESOLUTION_SYSTEM_PROMPT,
        user_content=_resolution_user_content(discrepancies),
        model=model,
        max_tokens=1024,
    )

    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
    try:
        resolutions = json.loads(text)
    except json.JSONDecodeError as e:
        raise click.ClickException(
            f"Failed to parse gate resolution response as JSON: {e}\nRaw response:\n{response[:500]}"
        )

    for d in discrepancies:
        resolution = resolutions.get(d.rule)
        if not resolution or not str(resolution).strip():
            raise click.ClickException(
                f"Gate resolution missing for discrepancy '{d.rule}'.\nRaw response:\n{response[:500]}"
            )
        d.resolution = str(resolution).strip()
    return discrepancies
