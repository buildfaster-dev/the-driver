# PRD: The Driver

## 1. Overview

### Problem Statement

Engineering Managers spend 45-60 minutes manually reviewing each candidate's technical test submission. With AI assistants now standard in development workflows, the evaluation challenge has shifted to two dimensions:

1. **Software Engineering Foundations** — Does the code demonstrate solid SE principles (clean architecture, testing, security, code quality) regardless of how it was produced?
2. **AI Orchestration Quality** — Is the candidate driving the AI with intent and expertise, or blindly copy-pasting output?

There is no standardized, repeatable process to evaluate these dimensions consistently across candidates and reviewers.

### Proposed Solution

**The Driver** — a CLI tool that accepts a candidate's Git repository, performs automated static analysis (Layer 1) and an AI-powered expert code review (Layer 2), and generates a structured `report.md` (Layer 3) scoring the candidate across three pillars: Architecture Awareness, Code Refinement, and Edge Case Coverage.

### Goals & Success Metrics

| Goal | Metric | Target |
|------|--------|--------|
| Reduce review time | Time from repo submission to actionable report | < 5 minutes |
| Evaluation consistency | Score variance across repeated runs on same repo | < 10% |
| Actionable output | Report includes scores, justification, and recommendation | 100% of reports |
| MVP delivery | Working CLI with all three layers | 6 hours |

---

## 2. User Personas

### Primary: Engineering Manager / Hiring Manager

- **Profile**: Senior engineer (5+ years) in a management role, reviews 10-30 technical tests per hiring cycle
- **Pain Points**:
  - Manual reviews are time-consuming and inconsistent
  - Hard to distinguish "good AI usage" from "copy-paste AI usage"
  - No framework to evaluate SE foundations in AI-assisted submissions
  - Difficulty articulating rejection reasons with evidence
- **Goals**:
  - Quickly identify strong candidates ("AI Orchestrators")
  - Have a defensible, shareable report for hiring committee discussions
  - Spend review time on decision-making, not analysis

### Jobs-to-be-Done (JTBD)

1. **When** I receive a candidate's technical test repo, **I want to** run a single command and get a structured assessment, **so that** I can decide in minutes whether to advance them.
2. **When** I discuss a candidate with my team, **I want to** share a report with objective metrics and expert scores, **so that** we have a common basis for comparison.
3. **When** I reject a candidate, **I want to** reference specific findings from the report, **so that** I can provide constructive, evidence-based feedback.

---

## 3. User Stories

| ID | Priority | Story | Acceptance Criteria |
|----|----------|-------|-------------------|
| US-01 | **Must** | As an EM, I want to run `the-driver analyze <repo-path>` and get a `report.md`, so I can quickly assess a candidate. | CLI accepts a local repo path, runs analysis, and outputs `report.md` within 5 minutes. |
| US-02 | **Must** | As an EM, I want to see objective code metrics in the report, so I have data-driven evidence. | Report includes: test file presence/ratio, linter config detection, commit count and message quality, dependency summary, error handling pattern analysis, security flag count. |
| US-03 | **Must** | As an EM, I want an AI-generated expert assessment with scores on the Three Pillars, so I can evaluate SE depth and AI usage quality. | Report includes 1-5 scores for Architecture Awareness, Code Refinement, and Edge Case Coverage, each with written justification citing specific code evidence. |
| US-04 | **Must** | As an EM, I want a final classification and recommendation in the report, so I have a clear takeaway. | Report concludes with classification (Copy-Paster / Assisted Engineer / AI Orchestrator) and recommendation (Pass / Review Further / Reject). |
| US-05 | **Should** | As an EM, I want to specify candidate name and repo URL as CLI options, so the report is self-contained and identifiable. | CLI accepts `--candidate <name>` and `--repo-url <url>` flags; values appear in report header. |
| US-06 | **Should** | As an EM, I want to specify an output path for the report, so I can organize reports by candidate. | CLI accepts `--output <path>` flag; defaults to `./report.md`. |
| US-07 | **Could** | As an EM, I want to provide a config file with custom evaluation criteria, so I can adapt scoring to my team's standards. | CLI accepts `--config <path>` with custom weights or rules. |
| US-08 | **Won't** | As an EM, I want to upload AI chat history for delta analysis. | Deferred to post-MVP. |
| US-09 | **Won't** | As an EM, I want a web dashboard to browse and compare reports. | Deferred to post-MVP. |

---

## 4. Functional Requirements

### Core Features (MVP)

**FR-01: CLI Interface**
- Command: `the-driver analyze <repo-path>`
- Options: `--candidate <name>`, `--repo-url <url>`, `--output <path>`, `--model <model>` (default: sonnet)
- Progress feedback during analysis
- Exit codes: 0 (success), 1 (error)

**FR-02: Layer 1 — Automated Repo Scan**
- Detect project language/framework from file extensions and config files
- Test analysis: presence of test files, test-to-source ratio
- Linter/formatter detection: presence of config files (.eslintrc, .prettierrc, pylint, etc.)
- Commit history: total commits, commit message quality (conventional commits, descriptive vs. "fix"), commit cadence
- Dependency audit: detect package manager, list dependencies, flag obviously outdated or unnecessary packages
- Error handling: detect blanket try/catch vs. strategic error handling patterns
- Security: scan for hardcoded secrets, common vulnerability patterns (regex-based)

**FR-03: Layer 2 — AI Agent Review**
- Send codebase context to AI model (Claude API)
- Score each pillar (1-5) with written justification:
  - **Architecture Awareness**: project structure, separation of concerns, design patterns, naming, appropriate use of abstractions
  - **Code Refinement**: code cleanliness, idiomatic usage, absence of unnecessary boilerplate, appropriate library choices
  - **Edge Case Coverage**: input validation, error boundaries, test coverage of edge cases, security considerations
- Cite specific files/functions as evidence

**FR-04: Layer 3 — Report Generation**
- Generate `report.md` combining Layer 1 and Layer 2
- Sections: Header (candidate, repo, date), Metrics Summary, Pillar Scores, Classification, Recommendation
- Classification logic:
  - Average score <= 2: **Copy-Paster**
  - Average score 3: **Assisted Engineer**
  - Average score >= 4: **AI Orchestrator**
- Recommendation derived from classification + metrics flags

### Future Features (Post-MVP)

- AI chat history import (ChatGPT/Claude JSON exports) for Delta Analysis
- Prompt Lineage Map visualization
- Correction Score calculation
- Custom company linter/security rule integration
- Batch processing of multiple repos
- Web dashboard for report browsing and candidate comparison
- ATS (Applicant Tracking System) integration

### Out of Scope

- Real-time monitoring during candidate tests
- AI chat history capture or recording
- Code execution or runtime analysis
- Plagiarism detection
- Candidate surveillance of any kind
- GUI/web interface (MVP is CLI only)

---

## 5. Non-Functional Requirements

### Performance
- Full analysis of a typical test repo (< 50 files) completes in under 5 minutes
- CLI provides progress indicators during each layer's execution

### Security
- Candidate code is not persisted beyond the analysis session
- AI API keys stored via environment variables only
- No candidate data sent to third parties beyond the AI model API call
- Report output is local-only

### Scalability
- MVP: single repo, single run — no concurrency requirements
- Architecture should not preclude future batch processing

---

## 6. Assumptions & Dependencies

### Assumptions
- Candidates submit functional Git repositories with at least some commit history
- An AI model (Claude) can reliably assess code quality when given a structured rubric and code context
- Engineering Managers are comfortable using CLI tools
- Technical test repos are self-contained projects (not monorepo fragments)
- 6-hour timeline requires aggressive scope control — MVP focuses on core flow only

### Dependencies
- **Claude API** (Anthropic) for Layer 2 AI agent review
- **Git CLI** for repository analysis and commit history extraction
- **Python** runtime
- Language-specific analysis tools for Layer 1 (linters, package managers)

---

## 7. Open Questions

| # | Question | Impact | Decision |
|---|----------|--------|----------|
| 1 | Which tech stack for the CLI? | Development speed, ecosystem access | **Python** |
| 2 | How to handle multi-language repos in Layer 1? | Scope of automated analysis | **Analyze all languages** — detect and scan whatever the candidate submitted |
| 3 | What is the minimum commit history for meaningful analysis? | Input validation, edge cases | **No minimum** — always analyze, flag low commit count as a warning in the report |
| 4 | Should the report include inline code snippets as evidence? | Report quality vs. length | **Yes** — include code snippets citing specific files/lines |
| 5 | How to validate AI scoring accuracy and consistency? | Quality assurance of Layer 2 | **Manual calibration** — run on 3-5 previously reviewed repos, compare against EM judgment, adjust prompt/rubric until aligned |
| 6 | What Claude model to use? | Cost, speed, quality tradeoff | **Sonnet 4.6 default**, configurable via `--model` flag (supports Opus for deeper analysis) |
| 7 | Should classification thresholds be configurable? | Flexibility vs. simplicity | **No** — hardcode for MVP (≤2 Copy-Paster, 3 Assisted Engineer, ≥4 AI Orchestrator), revisit after real usage data |
