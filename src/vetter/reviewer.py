import json
import re
from pathlib import PurePosixPath

import click
from vetter import router
from vetter.models import FileInfo, RepoData, ReviewResult, PillarScore


SYSTEM_PROMPT = """You are a Staff Software Engineer conducting a code review of a candidate's technical test submission.

Evaluate the codebase across three pillars, scoring each from 1 to 5:

## Pillar 1: Architecture Awareness (1-5)
Evaluate project structure, separation of concerns, design patterns, naming conventions, and appropriate use of abstractions.
- 1: No structure, everything in one file, no patterns
- 2: Minimal structure, poor separation, inconsistent naming
- 3: Basic structure present, some patterns, acceptable naming
- 4: Well-organized, clear separation, good patterns, consistent naming
- 5: Excellent architecture, strong design patterns, clean abstractions

## Pillar 2: Code Refinement (1-5)
Evaluate code cleanliness, idiomatic usage, absence of unnecessary boilerplate, and appropriate library choices.
- 1: Raw AI-generated boilerplate, no cleanup, poor idioms
- 2: Mostly boilerplate, some cleanup, inconsistent style
- 3: Reasonable code, some boilerplate remains, acceptable idioms
- 4: Clean code, idiomatic, good library choices, minimal boilerplate
- 5: Highly refined, excellent idioms, thoughtful library usage

## Pillar 3: Edge Case Coverage (1-5)
Evaluate input validation, error handling, test coverage of boundary conditions, and security considerations.
- 1: No error handling, no tests, no input validation
- 2: Minimal error handling, few tests, basic validation
- 3: Some error handling, tests for happy path, basic validation
- 4: Good error handling, tests include edge cases, proper validation
- 5: Comprehensive error handling, thorough edge case testing, security-aware

## Response Format
Respond ONLY with valid JSON in this exact format:
{
  "architecture_awareness": {
    "score": <1-5>,
    "justification": "<2-3 sentences explaining the score>",
    "evidence": ["<file:line — specific code reference>", "..."]
  },
  "code_refinement": {
    "score": <1-5>,
    "justification": "<2-3 sentences explaining the score>",
    "evidence": ["<file:line — specific code reference>", "..."]
  },
  "edge_case_coverage": {
    "score": <1-5>,
    "justification": "<2-3 sentences explaining the score>",
    "evidence": ["<file:line — specific code reference>", "..."]
  },
  "overall_summary": "<3-5 sentence overall assessment of the candidate's engineering quality>"
}"""


CONTEXT_CHAR_BUDGET = 400_000

# Signal-scoring weights. The order of precedence is deliberate and explicit:
# ENTRY_POINT_BONUS > FAN_IN_CAP, so an entry point with zero incoming
# references still outranks any pure fan-in hub — being the place where the
# system starts is stronger evidence of signal than being widely imported.
ENTRY_POINT_BONUS = 6
CONFIG_MODULE_BONUS = 4
FAN_IN_CAP = 5  # +1 per referencing file, capped here
ROOT_LEVEL_BONUS = 1  # file sits at the repo root

ENTRY_POINT_STEMS = {"main", "app", "cli", "index", "server", "application", "__main__"}
CONFIG_MODULE_STEMS = {"settings", "config", "conf"}

# Cross-language approximation of "lines that reference other modules".
# "alias " is Elixir's module reference — without it, every Elixir repo
# reads as zero fan-in and signal ranking collapses to size.
_IMPORT_LINE_PREFIXES = ("import ", "from ", "require", "use ", "using ", "include ", "alias ")


def _import_lines(content: str) -> str:
    return "\n".join(
        line for line in content.splitlines() if line.lstrip().startswith(_IMPORT_LINE_PREFIXES)
    )


def _fan_in_counts(files: list[FileInfo]) -> dict[str, int]:
    """How many OTHER files reference each file's stem in their import lines.

    Word-boundary match on stems of >=3 chars — an approximation that trades
    precision for language-agnosticism (no parser per language).
    """
    import_texts = {f.path: _import_lines(f.content) for f in files}
    counts: dict[str, int] = {}
    for f in files:
        stem = PurePosixPath(f.path).stem
        if len(stem) < 3:
            counts[f.path] = 0
            continue
        # Match the stem as written and as PascalCase: Elixir/Ruby/etc. name
        # the module CircuitBreaker while the file is circuit_breaker.ex.
        pascal = "".join(part.capitalize() for part in stem.split("_"))
        pattern = re.compile(rf"\b({re.escape(stem)}|{re.escape(pascal)})\b")
        counts[f.path] = sum(
            1 for path, text in import_texts.items() if path != f.path and pattern.search(text)
        )
    return counts


def _signal_score(f: FileInfo, fan_in: int) -> int:
    stem = PurePosixPath(f.path).stem.lower()
    score = min(fan_in, FAN_IN_CAP)
    if stem in ENTRY_POINT_STEMS:
        score += ENTRY_POINT_BONUS
    if stem in CONFIG_MODULE_STEMS:
        score += CONFIG_MODULE_BONUS
    if len(PurePosixPath(f.path).parts) == 1:
        score += ROOT_LEVEL_BONUS
    return score


def _rank_by_signal(files: list[FileInfo], fan_in_counts: dict[str, int]) -> list[FileInfo]:
    """Highest signal first; among equals, smallest first (path as final tiebreak)."""
    return sorted(
        files, key=lambda f: (-_signal_score(f, fan_in_counts[f.path]), f.size, f.path)
    )


def _render_file_block(f: FileInfo) -> str:
    return f"\n### {f.path}\n```{f.language.lower()}\n{f.content}\n```"


def _build_codebase_context(repo_data: RepoData, char_budget: int = CONTEXT_CHAR_BUDGET) -> str:
    """Build the review context, filling the budget by signal instead of size.

    The full rendered block (header + fences + content) is charged against the
    budget. Files that don't fit are announced in a final omitted-files list —
    one short line each, exempt from the budget so the announcement can never
    be silently dropped.
    """
    parts = []

    parts.append("## File Tree")
    for f in sorted(repo_data.files, key=lambda x: x.path):
        marker = " [TEST]" if f.is_test else ""
        parts.append(f"  {f.path} ({f.language}, {f.size}B){marker}")

    parts.append(f"\n## Languages: {repo_data.languages}")
    parts.append(f"## Total Files: {repo_data.total_files}, Total Lines: {repo_data.total_lines}")

    parts.append("\n## Commit History (most recent first)")
    for commit in repo_data.commits[:30]:
        parts.append(f"  [{commit.hash}] {commit.message} (by {commit.author}, +{commit.insertions}/-{commit.deletions})")

    source_files = [f for f in repo_data.files if not f.is_test and f.language not in ("JSON", "YAML", "TOML", "Markdown")]
    test_files = [f for f in repo_data.files if f.is_test]
    fan_in = _fan_in_counts(repo_data.files)

    chars_used = 0
    omitted: list[FileInfo] = []

    parts.append("\n## Source Files")
    for f in _rank_by_signal(source_files, fan_in):
        block = _render_file_block(f)
        if chars_used + len(block) > char_budget:
            omitted.append(f)
            continue
        parts.append(block)
        chars_used += len(block)

    parts.append("\n## Test Files")
    for f in _rank_by_signal(test_files, fan_in):
        block = _render_file_block(f)
        if chars_used + len(block) > char_budget:
            omitted.append(f)
            continue
        parts.append(block)
        chars_used += len(block)

    if omitted:
        parts.append("\n## Files Omitted from Context (over budget)")
        for f in omitted:
            parts.append(f"  {f.path} ({f.language}, {f.size}B)")

    return "\n".join(parts)


def _clamp_score(value) -> int:
    """Ensure score is an integer in 1-5 range."""
    score = int(round(value))
    return max(1, min(5, score))


def _parse_review_response(response_text: str) -> ReviewResult:
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise click.ClickException(
            f"Failed to parse AI review response as JSON: {e}\n"
            f"Raw response:\n{response_text[:500]}"
        )

    try:
        return ReviewResult(
            architecture_awareness=PillarScore(
                name="Architecture Awareness",
                score=_clamp_score(data["architecture_awareness"]["score"]),
                justification=data["architecture_awareness"]["justification"],
                evidence=data["architecture_awareness"].get("evidence", []),
            ),
            code_refinement=PillarScore(
                name="Code Refinement",
                score=_clamp_score(data["code_refinement"]["score"]),
                justification=data["code_refinement"]["justification"],
                evidence=data["code_refinement"].get("evidence", []),
            ),
            edge_case_coverage=PillarScore(
                name="Edge Case Coverage",
                score=_clamp_score(data["edge_case_coverage"]["score"]),
                justification=data["edge_case_coverage"]["justification"],
                evidence=data["edge_case_coverage"].get("evidence", []),
            ),
            overall_summary=data["overall_summary"],
        )
    except KeyError as e:
        raise click.ClickException(
            f"AI review response missing expected field: {e}\n"
            f"Raw response:\n{response_text[:500]}"
        )


def _validate_review_result(result: ReviewResult, raw_response: str) -> None:
    """Reject deliveries with empty justification/evidence — delivery contract, not judgment."""
    pillars = (result.architecture_awareness, result.code_refinement, result.edge_case_coverage)
    for pillar in pillars:
        if not pillar.justification.strip():
            raise click.ClickException(
                f"AI review has an empty justification for pillar '{pillar.name}'.\n"
                f"Raw response:\n{raw_response[:500]}"
            )
        if not [e for e in pillar.evidence if e.strip()]:
            raise click.ClickException(
                f"AI review has no evidence for pillar '{pillar.name}'.\n"
                f"Raw response:\n{raw_response[:500]}"
            )
    if not result.overall_summary.strip():
        raise click.ClickException(
            f"AI review has an empty overall_summary.\nRaw response:\n{raw_response[:500]}"
        )


def review_repo(repo_data: RepoData, model: str = "sonnet") -> ReviewResult:
    context = _build_codebase_context(repo_data)
    response_text = router.complete(
        system=SYSTEM_PROMPT,
        user_content=f"Review this candidate's technical test submission:\n\n{context}",
        model=model,
    )
    result = _parse_review_response(response_text)
    _validate_review_result(result, response_text)
    return result
