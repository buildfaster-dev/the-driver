# The Driver

An AI-powered code review CLI tool for Engineering Managers evaluating software engineer candidates.

## Problem

Engineering Managers spend excessive time reviewing technical tests from candidates who use AI assistants. The challenge isn't detecting AI usage — it's expected. The real challenge is twofold:

1. **Evaluating software engineering foundation**: Does the candidate demonstrate solid SE principles — clean architecture, proper testing, security awareness, code quality — regardless of how the code was produced?
2. **Evaluating AI orchestration skills**: Is the candidate an **"AI Orchestrator"** (drives the AI with architectural thinking, refactors output, handles edge cases) or a **"Copy-Paster"** (blindly accepts AI output)?

## Evaluation Framework: The Three Pillars

### 1. Architecture Awareness (Prompt-to-Logic Ratio)

- **The Sign**: The candidate shouldn't just prompt "Write a login page."
- **Best Practice**: They provide the AI with architectural constraints first.
- _Example_: "Generate a React component for login, using our specific AuthContext, with yup for validation, ensuring we handle 429 Rate Limit errors specifically."
- **What it proves**: They understand the system design; the AI is just the "typist."

### 2. Code Refinement (AI-Hallucination Filtering)

- **The Sign**: AI code is often boilerplate-heavy or uses outdated libraries.
- **Best Practice**: The engineer deletes or modifies ~20-30% of what the AI produced.
- _Example_: Removing unnecessary try/catch blocks, replacing generic loops with performant internal utilities.
- **What it proves**: They are reading the code, not just executing it. They have the "Foundational Veto."

### 3. Edge Case Coverage (Defensive Prompting)

- **The Sign**: Junior AI users forget the "unhappy path."
- **Best Practice**: The candidate handles security and edge cases.
- _Example_: Unit tests for null pointers, SQL injection patterns, and high-concurrency race conditions.
- **What it proves**: They know what can go wrong in production.

## MVP Flow

1. Candidate submits a Git repo (technical test solution)
2. The Driver analyzes the repo automatically
3. The Driver generates a `report.md` with structured assessment

## Solution: Three-Layer Architecture

### Layer 1: Automated Repo Scan (Objective Metrics)

Static analysis that objectively measures:
- Test coverage presence and ratio
- Linter/formatting consistency
- Commit history quality (granular commits vs. single "initial commit" dump)
- Dependency choices (outdated? unnecessary?)
- Error handling patterns (strategic vs. blanket try/catch)
- Security basics (hardcoded secrets, SQL injection vectors)

### Layer 2: AI Agent Review (SE Expert Judgment)

A software engineering expert agent that analyzes the code and scores each pillar automatically:
- **Architecture Awareness** (1-5): System design thinking vs. "make it work"
- **Code Refinement** (1-5): Clean and intentional vs. raw AI boilerplate
- **Edge Case Coverage** (1-5): Unhappy paths handled? Boundary tests?

### Layer 3: Generated report.md

Combines both layers into a structured report:
- Candidate info + repo link
- Automated metrics summary (Layer 1)
- Scored rubric with analysis (Layer 2)
- Final classification: **Copy-Paster** / **Assisted Engineer** / **AI Orchestrator**
- Recommendation: Pass / Review Further / Reject

## Technical Decisions

- **Interface**: CLI tool
- **Commits**: Conventional commits

## Input

- A Git repository (the candidate's technical test submission)

## Output

- `report.md` — structured, shareable candidate assessment

## Future Vision

- JSON export of AI chat history (ChatGPT/Claude/Copilot) for Delta Analysis
- Prompt Lineage Map: visual timeline of Prompt -> Code -> Edit -> Refinement
- Correction Score: how many times the human corrected AI logic
- Dependency Alignment: flag code that violates company-specific linter/security rules
