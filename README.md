# Vetter

AI-powered code review CLI for technical hiring.

Analyzes a candidate's Git repository and generates a structured `report.md` evaluating **software engineering foundations** and **AI orchestration skills** across three pillars:

1. **Architecture Awareness** — Project structure, separation of concerns, design patterns
2. **Code Refinement** — Code cleanliness, idiomatic usage, absence of boilerplate
3. **Edge Case Coverage** — Error handling, test coverage, security considerations

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/buildfaster-dev/vetter-cli.git
cd vetter-cli
uv sync
```

## Usage

Vetter analyzes **local repositories only**. Clone the candidate's repo first, then point Vetter at it.

```bash
export ANTHROPIC_API_KEY=your-key-here

# Clone and analyze
git clone https://github.com/candidate/repo.git
uv run vetter analyze ./repo

# Or analyze a repo already on disk
uv run vetter analyze /path/to/candidate/repo
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | `sonnet` | Claude model: `sonnet` (faster, cheaper) or `opus` (deeper analysis) |
| `--output` | `./report.md` | Output file path |
| `--candidate` | — | Candidate name (report header only, does not affect analysis) |
| `--repo-url` | — | Repository URL (report header only — does not clone) |

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
- Classification: **Copy-Paster** / **Assisted Engineer** / **AI Orchestrator**
- Recommendation: **Reject** / **Review Further** / **Pass**
- Pillar scores with justification
- Metrics summary

## Example Output

```
## Classification

| Metric | Value |
|--------|-------|
| Average Pillar Score | 4.0 / 5 |
| Classification | AI Orchestrator |
| Recommendation | Pass |
```

## Evaluating Vetter Itself

Vetter ships with an evaluation harness that measures Vetter against a **golden set** of repositories you have pre-labeled. Instead of "I think the prompt got better," you get "agreement dropped on 1 of 3 repos" — the difference between a hunch and a measurement.

```bash
# Copy the documented template and edit it with your own repos and labels
cp golden_set.example.json golden_set.json

uv run vetter eval                       # runs ./golden_set.json
uv run vetter eval --golden-set my.json  # or a set you name
```

Each specimen declares the classification, recommendation, and (optionally) per-pillar score ranges **you** consider correct. The harness runs the full pipeline against each repo and reports, per specimen:

- **Agreement** — obtained classification vs. your label (`AGREE` / `DISAGREE`).
- **Consistency** — per-pillar score spread across repeated runs (`temperature=0` is not determinism; the harness measures it rather than hiding it).
- **Evidence validity** — flags any file path the review cited that does not exist in the analyzed repo.
- **Injection resistance** — flags repository content that tries to instruct the reviewer.

`vetter eval` exits non-zero when any specimen's classification disagrees with its label, so it doubles as a **regression gate**: run it before and after a prompt or heuristic change. Machine-readable results are written to `./eval-results/`.

Two synthetic specimens ship under `fixtures/` (a deliberately weak `copy-paster-js` repo and an `injection-probe` that embeds prompt-injection attempts) so the golden set has a low end and a security case out of the box. See `golden_set.example.json` for the schema and how to add your own repos.

## Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest -v

# Run the CLI
uv run vetter --help
```

## License

MIT
