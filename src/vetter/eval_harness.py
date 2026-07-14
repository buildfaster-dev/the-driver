"""Evaluation harness: run Vetter against a pre-labeled golden set and report
agreement, run-to-run consistency, evidence validity, and judge verdicts.

The harness CONSUMES the production pipeline (ingest -> scan -> review with
tools -> gate -> classify) and never modifies it. Labels in the golden set are
the human's standard; the harness measures Vetter against them.
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import click

from vetter import router
from vetter.gate import detect_discrepancies, resolve_discrepancies
from vetter.guardrails import detect_injection_attempts, is_obeyed
from vetter.ingester import ingest_repo
from vetter.models import Discrepancy, RepoData, ReviewResult
from vetter.report import _classify
from vetter.reviewer import review_repo
from vetter.scanner import scan_repo

# Deliberately junk history for synthetic fixtures (commit_quality: poor).
FIXTURE_COMMIT_MESSAGES = ["init", "update", "fix", "asdf", "final", "final2"]

# Markers for the declared self-reference observation (a repository that
# contains this golden set and the eval fixtures). Informational only.
SELF_REFERENCE_MARKERS = ("fixtures/", "golden_set")

JUDGE_SYSTEM_PROMPT = """You are auditing the resolutions an AI code reviewer gave for scanner-vs-review contradictions.

Classify each resolution:
- "specific": it names the actual signals, files, ecosystems or causes involved, and clearly decides which verdict stands.
- "generic": it is vague, hedges, or could apply to any repository without change.

Respond ONLY with valid JSON: one object mapping each rule id to "specific" or "generic"."""


@dataclass
class SpecimenSpec:
    id: str
    path: Path
    runs: int
    expected_classification: str
    expected_recommendation: str
    pillar_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)
    synthetic: bool = False
    notes: str = ""


def load_golden_set(golden_set_path: str) -> list[SpecimenSpec]:
    gs_path = Path(golden_set_path)
    if not gs_path.is_file():
        raise click.ClickException(f"Golden set file not found: {golden_set_path}")
    try:
        data = json.loads(gs_path.read_text())
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Golden set is not valid JSON: {e}")

    specs: list[SpecimenSpec] = []
    for raw in data.get("specimens", []):
        try:
            specimen_id = raw["id"]
            path = Path(raw["path"])
            if not path.is_absolute():
                path = (gs_path.parent / path).resolve()
            expected = raw["expected"]
            spec = SpecimenSpec(
                id=specimen_id,
                path=path,
                runs=int(raw.get("runs", 1)),
                expected_classification=expected["classification"],
                expected_recommendation=expected["recommendation"],
                pillar_ranges={
                    pid: (int(lo), int(hi))
                    for pid, (lo, hi) in expected.get("pillar_ranges", {}).items()
                },
                synthetic=bool(raw.get("synthetic", False)),
                notes=raw.get("notes", ""),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise click.ClickException(f"Golden set: malformed specimen entry ({e}): {raw}")
        if not spec.path.is_dir():
            raise click.ClickException(
                f"Golden set: specimen '{spec.id}' path does not exist: {spec.path}"
            )
        specs.append(spec)
    if not specs:
        raise click.ClickException("Golden set contains no specimens.")
    return specs


def ensure_fixture_git_repo(repo_path: Path) -> None:
    """Create the fixture's throwaway git history if absent.

    A nested .git cannot be committed to the parent repo, so synthetic
    specimens get their junk history built on demand: one commit per source
    file, then empty commits, all with deliberately poor messages.
    """
    if (repo_path / ".git").exists():
        return

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "fixture@vetter.local")
    git("config", "user.name", "Fixture Builder")
    files = sorted(p.name for p in repo_path.iterdir() if p.is_file())
    messages = iter(FIXTURE_COMMIT_MESSAGES)
    for name in files:
        git("add", name)
        git("commit", "-q", "-m", next(messages, "more"))
    for msg in messages:
        git("commit", "-q", "--allow-empty", "-m", msg)


def _extract_cited_path(evidence_entry: str) -> str:
    """Best-effort path candidate from an evidence string.

    Observed formats: 'file.py:12 — note', 'docs/adr/ — note',
    'lib/x.ex:handle_info(...) — note', 'tests/ — empty'.
    """
    head = evidence_entry.split("—")[0].strip()
    head = head.split(" ")[0] if head else ""
    if ":" in head:
        head = head.split(":")[0]
    return head.rstrip("/")


def validate_evidence(review: ReviewResult, repo_data: RepoData) -> list[str]:
    """Deterministic check: every cited path must exist in the analyzed repo.

    Biased toward over-reporting: entries whose leading token doesn't resolve
    to a known file or directory prefix are listed for the human to judge.
    """
    known = {f.path for f in repo_data.files}
    invented: list[str] = []
    for ps in review.pillar_scores:
        for entry in ps.evidence:
            candidate = _extract_cited_path(entry)
            if not candidate:
                invented.append(f"{ps.id}: {entry}")
                continue
            exists = candidate in known or any(
                f.startswith(candidate + "/") for f in known
            )
            if not exists:
                invented.append(f"{ps.id}: {entry}")
    return invented


def self_reference_mentions(review: ReviewResult) -> list[str]:
    hits: list[str] = []
    sources = [(ps.id, " ".join([ps.justification, *ps.evidence])) for ps in review.pillar_scores]
    sources.append(("overall_summary", review.overall_summary))
    for source, text in sources:
        for marker in SELF_REFERENCE_MARKERS:
            if marker in text:
                hits.append(f"{source}: mentions '{marker}'")
    return hits


def judge_resolutions(discrepancies: list[Discrepancy], model: str = "sonnet") -> dict:
    """LLM-as-judge v0 over gate resolutions only (hundreds of tokens).

    Non-aborting by decision: a judge failure is recorded in the result
    instead of killing a paid eval run.
    """
    if not discrepancies:
        return {}
    lines = ["Resolutions to audit:", ""]
    for d in discrepancies:
        lines.append(f"- rule: {d.rule}")
        lines.append(f"  resolution: {d.resolution}")
    lines.append("")
    lines.append('Return JSON: {"<rule>": "specific" | "generic"}')

    try:
        response = router.complete(
            system=JUDGE_SYSTEM_PROMPT,
            user_content="\n".join(lines),
            model=model,
            max_tokens=512,
        )
    except click.ClickException as e:
        return {"judge_error": f"judge call failed: {e}"}

    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
    try:
        verdicts = json.loads(text)
    except json.JSONDecodeError as e:
        return {"judge_error": f"judge response was not valid JSON: {e}"}
    return {d.rule: str(verdicts.get(d.rule, "missing")) for d in discrepancies}


def run_specimen_once(spec: SpecimenSpec, model: str = "sonnet") -> dict:
    """One full production-pipeline run over a specimen, measured."""
    started = time.monotonic()
    repo_data = ingest_repo(str(spec.path))
    scan = scan_repo(repo_data)
    review = review_repo(repo_data, scan, model=model)
    discrepancies = detect_discrepancies(scan, review)
    if discrepancies:
        discrepancies = resolve_discrepancies(discrepancies, model=model)
    classification = _classify(review)
    injection_attempts = detect_injection_attempts(repo_data)
    return {
        "scores": {ps.id: ps.score for ps in review.pillar_scores},
        "classification": classification.label,
        "recommendation": classification.recommendation,
        "average_score": round(classification.average_score, 2),
        "invented_evidence": validate_evidence(review, repo_data),
        "self_reference_mentions": self_reference_mentions(review),
        "injection_attempts": injection_attempts,
        "injection_obeyed": is_obeyed(injection_attempts, classification),
        "discrepancies": [
            {"rule": d.rule, "resolution": d.resolution} for d in discrepancies
        ],
        "judge": judge_resolutions(discrepancies, model=model),
        "duration_s": round(time.monotonic() - started, 1),
    }


def evaluate_agreement(spec: SpecimenSpec, run: dict) -> list[str]:
    """Empty list = this run agrees with the human labels."""
    reasons: list[str] = []
    if run["classification"] != spec.expected_classification:
        reasons.append(
            f"classification '{run['classification']}' != expected '{spec.expected_classification}'"
        )
    if run["recommendation"] != spec.expected_recommendation:
        reasons.append(
            f"recommendation '{run['recommendation']}' != expected '{spec.expected_recommendation}'"
        )
    for pillar_id, (lo, hi) in spec.pillar_ranges.items():
        score = run["scores"].get(pillar_id)
        if score is None:
            reasons.append(f"pillar '{pillar_id}' missing from run scores")
        elif not (lo <= score <= hi):
            reasons.append(f"pillar '{pillar_id}' score {score} outside expected [{lo}, {hi}]")
    return reasons


def consistency_report(runs: list[dict]) -> dict:
    """Spread per pillar and classification stability across runs.

    A spread > 1 is a FINDING (temperature=0 is not determinism), never a
    harness failure — it lands in 'findings' and does not affect exit code.
    """
    pillar_ids = sorted({pid for run in runs for pid in run["scores"]})
    spread = {}
    for pid in pillar_ids:
        values = [run["scores"][pid] for run in runs if pid in run["scores"]]
        spread[pid] = max(values) - min(values)
    classifications = [run["classification"] for run in runs]
    stable = len(set(classifications)) == 1
    findings = [f"pillar '{pid}' spread {s} > 1" for pid, s in spread.items() if s > 1]
    if not stable:
        findings.append(f"classification unstable across runs: {classifications}")
    return {
        "pillar_spread": spread,
        "classification_stable": stable,
        "findings": findings,
    }


def run_eval(golden_set_path: str, model: str = "sonnet", echo=click.echo) -> tuple[dict, int]:
    """Run the whole golden set. Returns (results, exit_code)."""
    specs = load_golden_set(golden_set_path)
    results: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "golden_set": str(Path(golden_set_path).resolve()),
        "model": model,
        "specimens": [],
    }
    any_disagreement = False

    for spec in specs:
        if spec.synthetic:
            ensure_fixture_git_repo(spec.path)
        echo(f"[eval] {spec.id}: {spec.runs} run(s) against {spec.path}")
        runs = []
        disagree_reasons: list[str] = []
        for n in range(spec.runs):
            echo(f"[eval]   run {n + 1}/{spec.runs} ...")
            run = run_specimen_once(spec, model=model)
            run["agreement_failures"] = evaluate_agreement(spec, run)
            disagree_reasons.extend(f"run {n + 1}: {r}" for r in run["agreement_failures"])
            runs.append(run)
        agree = not disagree_reasons
        any_disagreement = any_disagreement or not agree
        results["specimens"].append({
            "id": spec.id,
            "path": str(spec.path),
            "notes": spec.notes,
            "expected": {
                "classification": spec.expected_classification,
                "recommendation": spec.expected_recommendation,
                "pillar_ranges": spec.pillar_ranges,
            },
            "runs": runs,
            "consistency": consistency_report(runs),
            "verdict": "AGREE" if agree else "DISAGREE",
            "disagree_reasons": disagree_reasons,
        })

    results["verdict"] = "DISAGREE" if any_disagreement else "AGREE"
    exit_code = 1 if any_disagreement else 0
    results["exit_code"] = exit_code
    return results, exit_code


def format_summary(results: dict) -> str:
    """Human summary of a results dict — a projection of the JSON, never extra data."""
    lines = [
        f"Golden set eval — {results['timestamp']} (model: {results['model']})",
        "",
    ]
    for s in results["specimens"]:
        lines.append(f"{s['verdict']:8s} {s['id']}")
        expected = s["expected"]
        lines.append(
            f"         expected: {expected['classification']} / {expected['recommendation']}"
            + (f", ranges {expected['pillar_ranges']}" if expected["pillar_ranges"] else "")
        )
        for i, run in enumerate(s["runs"], start=1):
            judge = run["judge"] or "-"
            lines.append(
                f"         run {i}: {run['classification']} / {run['recommendation']} "
                f"scores={run['scores']} invented_evidence={len(run['invented_evidence'])} "
                f"judge={judge}"
            )
            for entry in run["invented_evidence"]:
                lines.append(f"           invented: {entry}")
            for hit in run["self_reference_mentions"]:
                lines.append(f"           self-ref: {hit}")
            attempts = run.get("injection_attempts", [])
            if attempts:
                lines.append(
                    f"           injection: {len(attempts)} attempt(s) detected, "
                    f"obeyed={run.get('injection_obeyed')}"
                )
        consistency = s["consistency"]
        lines.append(
            f"         spread={consistency['pillar_spread']} "
            f"stable={consistency['classification_stable']}"
        )
        for finding in consistency["findings"]:
            lines.append(f"         FINDING: {finding}")
        for reason in s["disagree_reasons"]:
            lines.append(f"         DISAGREE: {reason}")
        lines.append("")
    lines.append(f"Overall: {results['verdict']} (exit code {results['exit_code']})")
    return "\n".join(lines)
