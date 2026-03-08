# The Driver

AI-powered code review CLI for Engineering Managers evaluating software engineer candidates.

Analyzes a candidate's Git repository and generates a structured `report.md` evaluating **software engineering foundations** and **AI orchestration skills** across three pillars:

1. **Architecture Awareness** — Project structure, separation of concerns, design patterns
2. **Code Refinement** — Code cleanliness, idiomatic usage, absence of boilerplate
3. **Edge Case Coverage** — Error handling, test coverage, security considerations

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/buildfaster-dev/the-driver.git
cd the-driver
uv sync
```

## Usage

```bash
export ANTHROPIC_API_KEY=your-key-here

# Analyze a local repo
uv run the-driver analyze /path/to/candidate/repo
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | `sonnet` | Claude model: `sonnet` (faster, cheaper) or `opus` (deeper analysis) |
| `--output` | `./report.md` | Output file path |
| `--candidate` | Not specified | Candidate name for report header |
| `--repo-url` | — | Repository URL for report header |

`--candidate` and `--repo-url` are metadata that appear in the report header. They do not affect analysis.

## How It Works

### Layer 1: Automated Scan
Static analysis that objectively measures:
- Test coverage ratio
- Linter/formatter configuration
- Commit history quality and cadence
- Dependency audit
- Error handling patterns (strategic vs. blanket)
- Security scan (hardcoded secrets)

### Layer 2: AI Expert Review
Sends the codebase to Claude for expert evaluation. Scores each pillar (1-5) with written justification and code evidence.

### Layer 3: Report Generation
Combines both layers into a `report.md` with:
- Metrics summary
- Pillar scores with justification
- Classification: **Copy-Paster** / **Assisted Engineer** / **AI Orchestrator**
- Recommendation: **Reject** / **Review Further** / **Pass**

## Example Output

```
## Classification

| Metric | Value |
|--------|-------|
| Average Pillar Score | 4.0 / 5 |
| Classification | AI Orchestrator |
| Recommendation | Pass |
```

## Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest -v

# Run the CLI
uv run the-driver --help
```

## License

MIT
