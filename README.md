# The Driver

AI-powered code review CLI for Engineering Managers evaluating software engineer candidates.

Analyzes a candidate's Git repository and generates a structured `report.md` evaluating **software engineering foundations** and **AI orchestration skills** across three pillars:

1. **Architecture Awareness** — Project structure, separation of concerns, design patterns
2. **Code Refinement** — Code cleanliness, idiomatic usage, absence of boilerplate
3. **Edge Case Coverage** — Error handling, test coverage, security considerations

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/your-org/the-driver.git
cd the-driver
uv sync
```

## Usage

```bash
export ANTHROPIC_API_KEY=your-key-here

# Basic usage
uv run the-driver analyze /path/to/candidate/repo

# With all options
uv run the-driver analyze /path/to/repo \
  --candidate "Jane Doe" \
  --repo-url "https://github.com/candidate/repo" \
  --output ./reports/jane-doe.md \
  --model opus
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--candidate` | Not specified | Candidate name for report header |
| `--repo-url` | Local path | Repository URL for report header |
| `--output` | `./report.md` | Output file path |
| `--model` | `sonnet` | Claude model: `sonnet`, `opus`, or `haiku` |

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
