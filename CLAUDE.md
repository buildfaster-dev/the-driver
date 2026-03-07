# The Driver

AI-powered code review CLI for Engineering Managers. Analyzes a candidate's Git repo and generates a `report.md` evaluating SE foundations and AI orchestration skills. Python 3.12+, CLI-only. Status: pre-MVP.

## Architecture

```
CLI (Click) → Repo Ingester (GitPython) → ┬→ Layer 1: Scanner (static analysis)
                                           ├→ Layer 2: Reviewer (Claude API)
                                           └→ Layer 3: Report Generator (Jinja2) → report.md
```

**Data flow**: repo path → RepoData (in-memory) → ScanResult + ReviewResult → report.md

## Directory Structure

```
the-driver/
├── src/the_driver/
│   ├── cli.py              # Click entry point: `the-driver analyze`
│   ├── ingester.py         # Git repo → RepoData (files, commits, metadata)
│   ├── scanner.py          # Layer 1: test ratio, linter, commits, security
│   ├── reviewer.py         # Layer 2: Claude API → Three Pillars scores
│   ├── report.py           # Layer 3: Jinja2 → report.md
│   ├── models.py           # Dataclasses: RepoData, ScanResult, ReviewResult, etc.
│   └── templates/
│       └── report.md.j2    # Report markdown template
├── tests/
├── docs/                   # PRD, TDD, prompts (spec2prod)
├── pyproject.toml
└── CLAUDE.md
```

## Tech Stack

- **CLI**: Click >=8.0
- **AI**: Anthropic SDK >=0.40 (Sonnet 4.6 default, `--model` for Opus)
- **Git**: GitPython >=3.1
- **Templates**: Jinja2 >=3.1
- **Progress**: Rich >=13.0
- **Package manager**: uv

## CLI Interface

```
the-driver analyze <repo-path> [OPTIONS]

Options:
  --candidate TEXT    Candidate name for report header
  --repo-url TEXT     Repository URL for report header
  --output TEXT       Output path (default: ./report.md)
  --model TEXT        Claude model (default: sonnet)
```

## Key Data Models (models.py)

- `RepoData`: files (FileInfo[]), commits (CommitInfo[]), languages, totals
- `ScanResult`: test_ratio, linter configs, commit quality, security flags
- `PillarScore`: name, score (1-5), justification, evidence (code snippets)
- `ReviewResult`: architecture_awareness, code_refinement, edge_case_coverage
- `Classification`: label (Copy-Paster/Assisted Engineer/AI Orchestrator), recommendation (Pass/Review Further/Reject)

## Classification Logic

- Average pillar score ≤ 2 → **Copy-Paster** → Reject
- Average pillar score 3 → **Assisted Engineer** → Review Further
- Average pillar score ≥ 4 → **AI Orchestrator** → Pass

## Three Pillars (Scoring Rubric)

1. **Architecture Awareness** (1-5): Project structure, separation of concerns, design patterns, naming
2. **Code Refinement** (1-5): Cleanliness, idiomatic usage, no unnecessary boilerplate, good library choices
3. **Edge Case Coverage** (1-5): Input validation, error handling, test boundaries, security

## Development Commands

```bash
uv sync                              # Install dependencies
uv run the-driver analyze <path>     # Run analysis
uv run pytest                        # Run tests
```

## Code Conventions

- Python 3.12+, type hints on all functions
- Dataclasses for data structures (no Pydantic in MVP)
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Click for CLI (not argparse, not typer)
- `ANTHROPIC_API_KEY` env var for API auth
- Temperature=0 for AI scoring consistency
- Structured JSON response from Claude, parsed into dataclasses

## Anti-patterns to Avoid

- Do NOT add a database — all data is ephemeral per run
- Do NOT build an HTTP API — this is CLI only
- Do NOT add async — keep it simple and synchronous
- Do NOT persist candidate code beyond the analysis session
- Do NOT log or include API keys in reports

## Implementation Phases

1. **Skeleton** (Hour 1): Project setup, CLI, dataclasses, ingester
2. **Scanner** (Hour 2): Layer 1 static analysis
3. **Reviewer** (Hour 2-3): Layer 2 Claude API integration
4. **Report** (Hour 4): Layer 3 Jinja2 template + generation
5. **Integration** (Hour 5): Pipeline wiring, error handling, Rich progress
6. **Calibration** (Hour 6): Test on real repos, adjust AI prompt
