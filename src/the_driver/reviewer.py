import json
import os
import click
import anthropic
from the_driver.models import RepoData, ReviewResult, PillarScore


MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

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


def _build_codebase_context(repo_data: RepoData) -> str:
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

    parts.append("\n## Source Files")
    source_files = [f for f in repo_data.files if not f.is_test and f.language not in ("JSON", "YAML", "TOML", "Markdown")]
    source_files.sort(key=lambda x: x.size, reverse=True)

    char_budget = 400_000
    chars_used = 0
    for f in source_files:
        if chars_used + len(f.content) > char_budget:
            parts.append(f"\n### {f.path} [TRUNCATED — file too large for context]")
            continue
        parts.append(f"\n### {f.path}")
        parts.append(f"```{f.language.lower()}")
        parts.append(f.content)
        parts.append("```")
        chars_used += len(f.content)

    test_files = [f for f in repo_data.files if f.is_test]
    test_files.sort(key=lambda x: x.size, reverse=True)

    parts.append("\n## Test Files")
    for f in test_files:
        if chars_used + len(f.content) > char_budget:
            parts.append(f"\n### {f.path} [TRUNCATED]")
            continue
        parts.append(f"\n### {f.path}")
        parts.append(f"```{f.language.lower()}")
        parts.append(f.content)
        parts.append("```")
        chars_used += len(f.content)

    return "\n".join(parts)


def _parse_review_response(response_text: str) -> ReviewResult:
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]

    data = json.loads(text)

    return ReviewResult(
        architecture_awareness=PillarScore(
            name="Architecture Awareness",
            score=data["architecture_awareness"]["score"],
            justification=data["architecture_awareness"]["justification"],
            evidence=data["architecture_awareness"].get("evidence", []),
        ),
        code_refinement=PillarScore(
            name="Code Refinement",
            score=data["code_refinement"]["score"],
            justification=data["code_refinement"]["justification"],
            evidence=data["code_refinement"].get("evidence", []),
        ),
        edge_case_coverage=PillarScore(
            name="Edge Case Coverage",
            score=data["edge_case_coverage"]["score"],
            justification=data["edge_case_coverage"]["justification"],
            evidence=data["edge_case_coverage"].get("evidence", []),
        ),
        overall_summary=data["overall_summary"],
    )


def review_repo(repo_data: RepoData, model: str = "sonnet") -> ReviewResult:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise click.ClickException("ANTHROPIC_API_KEY environment variable is not set.")

    model_id = MODEL_MAP.get(model, model)
    client = anthropic.Anthropic(api_key=api_key)
    context = _build_codebase_context(repo_data)

    message = client.messages.create(
        model=model_id,
        max_tokens=4096,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Review this candidate's technical test submission:\n\n{context}"}
        ],
    )

    response_text = message.content[0].text
    return _parse_review_response(response_text)
