"""Pillar interface: each scoring pillar declares itself once, and the system
prompt section, JSON schema fragment, and score parsing derive from it.

Adding a pillar = appending one Pillar to a list. Byte-parity with the
original hand-written SYSTEM_PROMPT is enforced by tests/test_pillars.py.
"""

from dataclasses import dataclass


# Written-out counts for the prompt header ("across three pillars").
_COUNT_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


@dataclass(frozen=True)
class Pillar:
    id: str  # JSON key and stable lookup id, e.g. "architecture_awareness"
    name: str  # display name, e.g. "Architecture Awareness"
    description: str  # the "Evaluate ..." line of its prompt section
    rubric: dict[int, str]  # score level (1-5) -> description

    def prompt_section(self, index: int) -> str:
        lines = [f"## Pillar {index}: {self.name} (1-5)", self.description]
        lines += [f"- {level}: {self.rubric[level]}" for level in sorted(self.rubric)]
        return "\n".join(lines)

    def schema_fragment(self) -> str:
        return (
            f'  "{self.id}": {{\n'
            '    "score": <1-5>,\n'
            '    "justification": "<2-3 sentences explaining the score>",\n'
            '    "evidence": ["<file:line — specific code reference>", "..."]\n'
            "  }"
        )


PILLARS = [
    Pillar(
        id="architecture_awareness",
        name="Architecture Awareness",
        description=(
            "Evaluate project structure, separation of concerns, design patterns, "
            "naming conventions, and appropriate use of abstractions."
        ),
        rubric={
            1: "No structure, everything in one file, no patterns",
            2: "Minimal structure, poor separation, inconsistent naming",
            3: "Basic structure present, some patterns, acceptable naming",
            4: "Well-organized, clear separation, good patterns, consistent naming",
            5: "Excellent architecture, strong design patterns, clean abstractions",
        },
    ),
    Pillar(
        id="code_refinement",
        name="Code Refinement",
        description=(
            "Evaluate code cleanliness, idiomatic usage, absence of unnecessary "
            "boilerplate, and appropriate library choices."
        ),
        rubric={
            1: "Raw AI-generated boilerplate, no cleanup, poor idioms",
            2: "Mostly boilerplate, some cleanup, inconsistent style",
            3: "Reasonable code, some boilerplate remains, acceptable idioms",
            4: "Clean code, idiomatic, good library choices, minimal boilerplate",
            5: "Highly refined, excellent idioms, thoughtful library usage",
        },
    ),
    Pillar(
        id="edge_case_coverage",
        name="Edge Case Coverage",
        description=(
            "Evaluate input validation, error handling, test coverage of boundary "
            "conditions, and security considerations."
        ),
        rubric={
            1: "No error handling, no tests, no input validation",
            2: "Minimal error handling, few tests, basic validation",
            3: "Some error handling, tests for happy path, basic validation",
            4: "Good error handling, tests include edge cases, proper validation",
            5: "Comprehensive error handling, thorough edge case testing, security-aware",
        },
    ),
]


def build_system_prompt(pillars: list[Pillar]) -> str:
    count = _COUNT_WORDS.get(len(pillars), str(len(pillars)))
    header = (
        "You are a Staff Software Engineer conducting a code review of a candidate's "
        "technical test submission.\n\n"
        f"Evaluate the codebase across {count} pillars, scoring each from 1 to 5:"
    )
    sections = [p.prompt_section(i) for i, p in enumerate(pillars, start=1)]
    response_format = (
        "## Response Format\n"
        "Respond ONLY with valid JSON in this exact format:\n"
        "{\n"
        + ",\n".join(p.schema_fragment() for p in pillars)
        + ",\n"
        '  "overall_summary": "<3-5 sentence overall assessment of the candidate\'s '
        'engineering quality>"\n'
        "}"
    )
    return "\n\n".join([header, *sections, response_format])
