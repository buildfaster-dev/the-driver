# Plan 003: MVP Hardening — Feature Audit & Robustness Fixes

## Context

All bootstrap phases (1-4) are complete. The pipeline works end-to-end on paper, but hasn't been validated against real repos. This plan audits every MVP feature against the PRD/TDD spec, identifies gaps, and defines the fixes needed to ship a reliable v0.1.0.

---

## MVP Feature Audit

### Legend
- ✅ Done — implemented and tested
- ⚠️ Partial — implemented but missing robustness/edge cases
- ❌ Missing — specified in PRD/TDD but not implemented

---

### FR-01: CLI Interface (`cli.py`)

| Feature | Status | Evidence | Gap |
|---------|--------|----------|-----|
| `vetter analyze <repo-path>` command | ✅ | cli.py:19-26 | — |
| `--candidate` option | ✅ | cli.py:21 | — |
| `--repo-url` option | ✅ | cli.py:22 | — |
| `--output` option (default: ./report.md) | ✅ | cli.py:23 | — |
| `--model` option (default: sonnet) | ✅ | cli.py:24 | — |
| Progress feedback during analysis | ✅ | cli.py:27-42, Rich spinners | — |
| Exit code 0 on success | ✅ | Click default | — |
| Exit code 1 on error | ⚠️ | Only via unhandled exceptions | No try-catch around pipeline; crashes with stack trace |
| Invalid repo path → user-friendly error | ⚠️ | Click `exists=True` checks path exists | Doesn't check if it's a *Git* repo — crashes with `InvalidGitRepositoryError` |
| Missing API key → user-friendly error | ✅ | reviewer.py:158-159 | — |
| Unwritable output path → user-friendly error | ❌ | cli.py:51 raw `open()` | No check, crashes with `PermissionError` |
| `--version` flag | ✅ | cli.py:13 | — |

**PRD ref**: FR-01, US-01

---

### FR-02: Layer 1 — Automated Repo Scan (`ingester.py` + `scanner.py`)

#### Ingester (`ingester.py`)

| Feature | Status | Evidence | Gap |
|---------|--------|----------|-----|
| Load Git repo from local path | ✅ | ingester.py:80 | — |
| Extract file tree | ✅ | ingester.py:86-119 | — |
| Read source file contents | ✅ | ingester.py:100-104 | — |
| Extract commit history | ✅ | ingester.py:121-132 | — |
| Skip binary files | ✅ | ingester.py:93, BINARY_EXTENSIONS | — |
| Skip vendor/generated dirs | ✅ | ingester.py:87, SKIP_DIRS | — |
| Limit file size (100KB) | ✅ | ingester.py:97-98, MAX_FILE_SIZE | — |
| Handle Unicode errors | ✅ | ingester.py:101, errors="ignore" | — |
| Detect languages from extensions | ✅ | ingester.py:59-61, LANGUAGE_MAP (25 extensions) | — |
| Detect test files | ✅ | ingester.py:64-66, TEST_PATTERNS | — |
| Validate path is a Git repo | ❌ | ingester.py:80, bare `Repo()` | No catch for `InvalidGitRepositoryError` |
| Limit commit iteration | ❌ | ingester.py:122, `iter_commits()` unbounded | Old repos with 10K+ commits will be very slow |

**PRD ref**: FR-02 (file extraction, commit history)

#### Scanner (`scanner.py`)

| Feature | Status | Evidence | Gap |
|---------|--------|----------|-----|
| Test-to-source ratio | ✅ | scanner.py:59-64 | — |
| Linter/formatter config detection | ✅ | scanner.py:5-18, 67-73 (18 configs) | — |
| Commit count | ✅ | scanner.py:143 | — |
| Commit message quality | ✅ | scanner.py:76-92 | Heuristic is basic (>10 chars + not in blocklist), but functional for MVP |
| Commit cadence analysis | ❌ | Not implemented | PRD mentions "commit cadence" but not critical for MVP |
| Dependency detection | ✅ | scanner.py:95-100, 10 package managers | — |
| Dependency audit (outdated/unnecessary) | ❌ | Not implemented | PRD says "flag obviously outdated or unnecessary packages" — deferred, requires version lookups |
| Error handling pattern detection | ✅ | scanner.py:103-120 | Covers Python, JS, Java. Missing Go, Rust |
| Security scan (hardcoded secrets) | ✅ | scanner.py:123-133, 8 regex patterns | — |
| Language detection via config files | ⚠️ | Via LANGUAGE_MAP extensions only | PRD says "file extensions and config files" — config-based detection not done |

**PRD ref**: FR-02, US-02

---

### FR-03: Layer 2 — AI Agent Review (`reviewer.py`)

| Feature | Status | Evidence | Gap |
|---------|--------|----------|-----|
| Send codebase context to Claude | ✅ | reviewer.py:64-109, 166-173 | — |
| Score Architecture Awareness (1-5) | ✅ | reviewer.py:17-24, system prompt | — |
| Score Code Refinement (1-5) | ✅ | reviewer.py:26-33, system prompt | — |
| Score Edge Case Coverage (1-5) | ✅ | reviewer.py:35-41, system prompt | — |
| Written justification per pillar | ✅ | reviewer.py:47-48 | — |
| Cite specific files as evidence | ✅ | reviewer.py:48 | — |
| Overall summary | ✅ | reviewer.py:60 | — |
| Temperature=0 for consistency | ✅ | reviewer.py:169 | — |
| Structured JSON response | ✅ | reviewer.py:43-61, system prompt | — |
| Parse JSON into dataclasses | ✅ | reviewer.py:112-153 | — |
| Handle markdown code block wrapping | ✅ | reviewer.py:114-117 | — |
| Token management (context truncation) | ✅ | reviewer.py:83-93, 400K char budget | — |
| Model selection (sonnet/opus/haiku) | ✅ | reviewer.py:8-12, MODEL_MAP | — |
| `AuthenticationError` handling | ✅ | reviewer.py:175-176 | — |
| `RateLimitError` handling | ⚠️ | reviewer.py:177-178 | Fails immediately; TDD says "retry once after 5 seconds" |
| `APIError` handling | ✅ | reviewer.py:179-180 | — |
| Invalid JSON response handling | ✅ | reviewer.py:121-125 | — |
| Missing fields handling | ✅ | reviewer.py:149-153 | — |
| Validate score range (1-5) | ❌ | Not implemented | AI could return 0, 6, or float — no clamp or validation |

**PRD ref**: FR-03, US-03, TDD §5.1

---

### FR-04: Layer 3 — Report Generation (`report.py` + `report.md.j2`)

| Feature | Status | Evidence | Gap |
|---------|--------|----------|-----|
| Generate report.md | ✅ | report.py:28-54 | — |
| Header: candidate, repo, date | ✅ | report.md.j2:1-11 | — |
| Metrics summary section | ✅ | report.md.j2:40-54 | — |
| Pillar scores table | ✅ | report.md.j2:24-31 | — |
| Overall summary section | ✅ | report.md.j2:34-36 | — |
| Evidence section per pillar | ✅ | report.md.j2:58-85 | — |
| Classification label | ✅ | report.py:7-25 | — |
| Recommendation | ✅ | report.py:7-25 | — |
| Classification logic (≤2/3/≥4) | ✅ | report.py:15-23 | — |
| Low commit count warning | ✅ | report.md.j2:52-54 | — |
| Blanket error handling flag | ✅ | report.md.j2:120-124 | — |
| Security flags section | ✅ | report.md.j2:128-134 | — |
| Scan details (linter, deps, commits) | ✅ | report.md.j2:89-114 | — |
| Jinja2 template separation | ✅ | templates/report.md.j2 | — |

**PRD ref**: FR-04, US-04

---

### US-05 to US-09 (User Stories)

| Story | Status | Notes |
|-------|--------|-------|
| US-05: `--candidate` and `--repo-url` in report | ✅ | — |
| US-06: `--output` path | ✅ | — |
| US-07: Config file with custom criteria | ❌ | PRD marks as "Could" — deferred |
| US-08: AI chat history import | ❌ | PRD marks as "Won't" — deferred |
| US-09: Web dashboard | ❌ | PRD marks as "Won't" — deferred |

---

### Non-Functional Requirements

| Requirement | Status | Gap |
|-------------|--------|-----|
| Analysis < 5 min for < 50 files | ⚠️ | Likely met but untested on real repos. Commit iteration has no cap. |
| Progress indicators | ✅ | Rich spinners per stage |
| No persisted candidate code | ✅ | All in-memory |
| API key via env var only | ✅ | — |
| No candidate data to third parties (except AI API) | ✅ | — |
| Report is local-only | ✅ | — |
| Input validation (valid Git repo) | ❌ | Only checks path exists, not that it's a Git repo |

**PRD ref**: §5

---

### Testing Coverage

| Area | Status | Gap |
|------|--------|-----|
| Ingester unit tests (helpers) | ✅ | 19 tests |
| Scanner unit tests | ✅ | 16 tests |
| Reviewer unit tests (parse, mock API) | ✅ | 7 tests |
| Report unit tests (classify, render) | ✅ | 10 tests |
| CLI integration tests | ❌ | Never test `vetter analyze` end-to-end |
| Error path tests (invalid repo, unwritable output) | ❌ | — |
| Real repo smoke test | ❌ | Never tested on a real repo with API |

**TDD ref**: §7

---

## Summary: What's Done vs. What's Not

### Done (32 features) ✅
- Full pipeline: CLI → Ingester → Scanner → Reviewer → Report
- All CLI options working
- Three Pillars scoring with structured JSON
- Classification logic
- Report template with all sections
- 52 unit tests passing
- API error handling (auth, rate limit, generic)
- Token budget management
- Binary/vendor/generated file skipping

### Partial (5 features) ⚠️
- Exit code 1 on error (crashes instead of clean exit)
- Rate limit handling (no retry, TDD specifies retry once)
- Language detection via config files (extension-only)
- Performance on large repos (no commit cap)
- Git repo validation (only path existence, not Git validity)

### Missing (6 features) ❌
- Git repo validation with user-friendly error
- Pillar score range validation (1-5 clamp)
- Unwritable output path error
- CLI integration tests
- Commit cadence analysis (PRD mentions, low priority)
- Dependency audit — outdated/unnecessary flags (PRD mentions, low priority)

### Deliberately Deferred (not MVP blockers)
- Commit cadence analysis — enhancement, not required for classification
- Dependency audit (outdated/unnecessary) — requires version lookups, post-MVP
- Language detection via config files — extensions are sufficient for MVP
- US-07 (config file) — PRD marks as "Could"
- US-08, US-09 — PRD marks as "Won't"

---

## Development Cycle

Following the project's defined cycle: **Spec → Implement → Test → Review**.

### 1. Spec — Define the change

**Scope**: Fix 5 partial + 4 missing items = 9 fixes. No new features, no behavior changes beyond what PRD/TDD already specify.

**PRD updates needed**: None — all fixes implement existing requirements that were underspecified in code, not in spec.

**TDD updates needed**:
- §5.1 (Integration Design > Error Handling): Already specifies retry once on RateLimitError — code just doesn't implement it yet
- §6 (Security): Already specifies input validation — code just doesn't implement it yet
- §7 (Testing): Already specifies CLI integration tests — just not written yet

**ADR needed**: No — these are bug fixes and missing validations, not architectural decisions.

**Changes by file**:

| File | Changes | PRD/TDD ref |
|------|---------|-------------|
| `src/vetter/ingester.py` | Git repo validation, commit cap (500) | TDD §6 input validation, NFR performance |
| `src/vetter/reviewer.py` | Score range validation (1-5 clamp), rate limit retry (5s, 1 retry) | TDD §5.1 error handling |
| `src/vetter/cli.py` | Output path validation, pipeline try-catch | FR-01 exit codes, TDD §6 |
| `tests/test_cli.py` | New — CLI integration tests | TDD §7 |
| `tests/test_reviewer.py` | Add score validation edge case tests | TDD §7 |

---

### 2. Implement — Write code following existing patterns

#### Fix 1: Validate Git repo in ingester (`ingester.py`)

Catch `InvalidGitRepositoryError` and `NoSuchPathError` from GitPython, raise `click.ClickException`.

```python
from git import Repo, InvalidGitRepositoryError, NoSuchPathError
import click

def ingest_repo(repo_path: str) -> RepoData:
    try:
        repo = Repo(repo_path)
    except InvalidGitRepositoryError:
        raise click.ClickException(f"Not a Git repository: {repo_path}")
    except NoSuchPathError:
        raise click.ClickException(f"Path does not exist: {repo_path}")
```

#### Fix 2: Cap commit iteration (`ingester.py`)

Limit `iter_commits()` to 500 most recent commits.

```python
MAX_COMMITS = 500

for commit in repo.iter_commits(max_count=MAX_COMMITS):
```

#### Fix 3: Validate pillar score range (`reviewer.py`)

Clamp scores to 1-5 integer range after parsing JSON response.

```python
def _clamp_score(value) -> int:
    """Ensure score is an integer in 1-5 range."""
    score = int(round(value))
    return max(1, min(5, score))
```

Apply in `_parse_review_response` when constructing each `PillarScore`.

#### Fix 4: Rate limit retry (`reviewer.py`)

On `RateLimitError`, wait 5 seconds and retry once (per TDD §5.1).

```python
import time

def review_repo(repo_data: RepoData, model: str = "sonnet") -> ReviewResult:
    # ... setup ...
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            message = client.messages.create(...)
            break
        except anthropic.RateLimitError:
            if attempt < max_attempts - 1:
                time.sleep(5)
                continue
            raise click.ClickException("Anthropic API rate limit reached. Please wait and try again.")
        except anthropic.AuthenticationError:
            raise click.ClickException(...)
        except anthropic.APIError as e:
            raise click.ClickException(...)
```

#### Fix 5: Validate output path (`cli.py`)

Before running the pipeline, check that the output directory exists.

```python
import os

output_dir = os.path.dirname(os.path.abspath(output))
if not os.path.isdir(output_dir):
    raise click.ClickException(f"Output directory does not exist: {output_dir}")
```

#### Fix 6: Pipeline try-catch (`cli.py`)

Wrap all layer calls so unhandled exceptions produce clean error messages with exit code 1.

```python
def analyze(repo_path, candidate, repo_url, output, model):
    """Analyze a local Git repository and generate a report."""
    try:
        # ... existing pipeline code ...
    except click.ClickException:
        raise  # Already formatted
    except FileNotFoundError as e:
        raise click.ClickException(f"File not found: {e}")
    except PermissionError as e:
        raise click.ClickException(f"Permission denied: {e}")
    except Exception as e:
        raise click.ClickException(f"Unexpected error: {e}")
```

#### Execution order

1. Fix 1 + Fix 2 — ingester (independent from other fixes)
2. Fix 3 + Fix 4 — reviewer (independent from other fixes)
3. Fix 5 + Fix 6 — cli (depends on Fix 1 for clean error propagation)

---

### 3. Test — Add/update tests

#### New file: `tests/test_cli.py` — CLI integration tests

Using Click's `CliRunner`:

| # | Test case | Expected |
|---|-----------|----------|
| 1 | `vetter --help` | Exit 0, shows "Vetter" |
| 2 | `vetter analyze --help` | Exit 0, shows options |
| 3 | `vetter analyze /nonexistent` | Exit ≠ 0, error about path |
| 4 | `vetter analyze /tmp` (non-Git dir) | Exit ≠ 0, "Not a Git repository" |
| 5 | Successful run (mock reviewer) | Exit 0, report file written |
| 6 | Missing API key (unset env var) | Exit ≠ 0, "ANTHROPIC_API_KEY" in message |

#### Updated file: `tests/test_reviewer.py` — Score validation tests

| # | Test case | Expected |
|---|-----------|----------|
| 1 | Score of 0 in JSON | Clamped to 1 |
| 2 | Score of 6 in JSON | Clamped to 5 |
| 3 | Score of 3.7 in JSON | Rounded to 4 |
| 4 | Score of 10 in JSON | Clamped to 5 |

#### Existing tests

All 52 existing tests must continue to pass unchanged.

```bash
uv run pytest -v    # Target: 52 existing + ~10 new = 62+ tests passing
```

---

### 4. Review — Verify against acceptance criteria

#### Acceptance criteria

- [ ] `vetter analyze /tmp` → "Not a Git repository", exit code 1
- [ ] `vetter analyze /nonexistent` → path error, exit code 1
- [ ] Missing `ANTHROPIC_API_KEY` → clean error, exit code 1
- [ ] `--output /nonexistent/dir/report.md` → "Output directory does not exist", exit code 1
- [ ] Pillar scores always 1-5 integers in report (even if AI returns out-of-range)
- [ ] Rate limit → retries once after 5s before failing
- [ ] Repos with 10K+ commits don't hang (capped at 500)
- [ ] All 62+ tests pass (`uv run pytest -v`)
- [ ] `vetter analyze --help` works

#### Manual verification commands

```bash
uv run pytest -v                              # All tests pass
uv run vetter analyze --help                  # Clean help output
uv run vetter analyze /tmp                    # "Not a Git repository" error
uv run vetter analyze --output /bad/path .    # "Output directory does not exist" error
uv run vetter analyze .                       # Works on this repo (if API key set)
```

#### Real repo smoke test

Run on vetter-cli itself (requires `ANTHROPIC_API_KEY`):

```bash
uv run vetter analyze . --candidate "Test" --output ./reports/self-review.md
```

Verify report has:
- Valid pillar scores (1-5)
- Classification and recommendation
- Evidence sections populated
- Scan metrics populated
