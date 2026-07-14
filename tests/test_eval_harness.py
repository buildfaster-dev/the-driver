import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from git import Repo

from vetter.cli import main
from vetter.eval_harness import (
    FIXTURE_COMMIT_MESSAGES,
    SpecimenSpec,
    consistency_report,
    ensure_fixture_git_repo,
    evaluate_agreement,
    judge_resolutions,
    load_golden_set,
    run_eval,
    self_reference_mentions,
    validate_evidence,
)
from vetter.models import Discrepancy, FileInfo, PillarScore, RepoData, ReviewResult


def _spec(**overrides):
    defaults = dict(
        id="specimen",
        path=Path("/tmp"),
        runs=1,
        expected_classification="AI Orchestrator",
        expected_recommendation="Pass",
        pillar_ranges={},
        synthetic=False,
    )
    defaults.update(overrides)
    return SpecimenSpec(**defaults)


def _run(scores=None, classification="AI Orchestrator", recommendation="Pass"):
    return {
        "scores": scores or {"architecture_awareness": 5, "code_refinement": 4, "edge_case_coverage": 5},
        "classification": classification,
        "recommendation": recommendation,
        "average_score": 4.7,
        "invented_evidence": [],
        "self_reference_mentions": [],
        "discrepancies": [],
        "judge": {},
        "duration_s": 1.0,
    }


def _review(evidence_by_pillar=None, summary="Fine."):
    evidence_by_pillar = evidence_by_pillar or {}
    return ReviewResult(
        pillar_scores=[
            PillarScore("architecture_awareness", "Architecture Awareness", 4, "Solid.",
                        evidence_by_pillar.get("architecture_awareness", [])),
            PillarScore("code_refinement", "Code Refinement", 4, "Clean.",
                        evidence_by_pillar.get("code_refinement", [])),
            PillarScore("edge_case_coverage", "Edge Case Coverage", 4, "Covered.",
                        evidence_by_pillar.get("edge_case_coverage", [])),
        ],
        overall_summary=summary,
    )


def _repo(paths):
    return RepoData(
        path="/analyzed/repo",
        files=[FileInfo(p, "x", "Python", 1, False) for p in paths],
        commits=[], languages={"Python": len(paths)},
        total_files=len(paths), total_lines=len(paths),
    )


def _write_golden_set(tmp_path, specimens):
    gs = tmp_path / "golden_set.json"
    gs.write_text(json.dumps({"version": 1, "specimens": specimens}))
    return str(gs)


class TestLoadGoldenSet:
    def test_loads_and_resolves_relative_paths(self, tmp_path):
        fixture_dir = tmp_path / "fixtures" / "bad-repo"
        fixture_dir.mkdir(parents=True)
        gs = _write_golden_set(tmp_path, [{
            "id": "bad-repo",
            "path": "fixtures/bad-repo",
            "runs": 3,
            "synthetic": True,
            "expected": {
                "classification": "Copy-Paster",
                "recommendation": "Reject",
                "pillar_ranges": {"edge_case_coverage": [1, 2]},
            },
        }])

        specs = load_golden_set(gs)

        assert specs[0].id == "bad-repo"
        assert specs[0].path == fixture_dir.resolve()
        assert specs[0].runs == 3
        assert specs[0].synthetic is True
        assert specs[0].pillar_ranges == {"edge_case_coverage": (1, 2)}

    def test_missing_specimen_path_fails_with_clear_message(self, tmp_path):
        gs = _write_golden_set(tmp_path, [{
            "id": "ghost",
            "path": "/nonexistent/repo/path",
            "expected": {"classification": "X", "recommendation": "Y"},
        }])
        with pytest.raises(click.ClickException, match="ghost.*does not exist"):
            load_golden_set(gs)

    def test_missing_golden_set_file(self):
        with pytest.raises(click.ClickException, match="not found"):
            load_golden_set("/nonexistent/golden_set.json")

    def test_malformed_specimen_entry(self, tmp_path):
        gs = _write_golden_set(tmp_path, [{"id": "broken"}])  # no path/expected
        with pytest.raises(click.ClickException, match="malformed specimen"):
            load_golden_set(gs)

    def test_invalid_json(self, tmp_path):
        gs = tmp_path / "golden_set.json"
        gs.write_text("{not json")
        with pytest.raises(click.ClickException, match="not valid JSON"):
            load_golden_set(str(gs))


class TestFixtureGitRepo:
    def test_builds_junk_history_once(self, tmp_path):
        fixture = tmp_path / "fixture"
        fixture.mkdir()
        (fixture / "app.js").write_text("var x = 1;\n")
        (fixture / "package.json").write_text("{}\n")

        ensure_fixture_git_repo(fixture)

        repo = Repo(fixture)
        messages = [c.message.strip() for c in repo.iter_commits()]
        assert len(messages) == len(FIXTURE_COMMIT_MESSAGES)
        assert set(messages) == set(FIXTURE_COMMIT_MESSAGES)

        head_before = repo.head.commit.hexsha
        ensure_fixture_git_repo(fixture)  # idempotent
        assert Repo(fixture).head.commit.hexsha == head_before


class TestEvidenceValidation:
    def test_known_formats_resolve_against_repo(self):
        repo = _repo(["src/app.py", "docs/adr/001-x.md", "lib/breaker.ex"])
        review = _review({
            "architecture_awareness": [
                "src/app.py:12 — clean entry point",
                "docs/adr/ — nine ADRs",
            ],
            "code_refinement": ["lib/breaker.ex:handle_info({:DOWN,...}) — crash recovery"],
        })
        assert validate_evidence(review, repo) == []

    def test_invented_paths_are_reported(self):
        repo = _repo(["src/app.py"])
        review = _review({
            "edge_case_coverage": [
                "ghost/module.py:1 — does not exist",
                "src/app.py:3 — real",
            ],
        })
        invented = validate_evidence(review, repo)
        assert invented == ["edge_case_coverage: ghost/module.py:1 — does not exist"]

    def test_proselike_entries_are_overreported_for_human_review(self):
        repo = _repo(["src/app.py"])
        review = _review({"architecture_awareness": ["The test suite is thorough"]})
        invented = validate_evidence(review, repo)
        assert len(invented) == 1  # deliberate bias: report, let the human judge


class TestSelfReference:
    def test_detects_declared_markers(self):
        review = _review(
            {"code_refinement": ["fixtures/copy-paster-js/app.js:1 — bad code"]},
            summary="Repo includes a golden_set.json for self-evaluation.",
        )
        hits = self_reference_mentions(review)
        assert any("code_refinement" in h and "fixtures/" in h for h in hits)
        assert any("overall_summary" in h and "golden_set" in h for h in hits)

    def test_clean_review_has_no_hits(self):
        assert self_reference_mentions(_review()) == []


class TestAgreement:
    def test_agreement_within_ranges(self):
        spec = _spec(pillar_ranges={"code_refinement": (3, 5)})
        assert evaluate_agreement(spec, _run()) == []

    def test_score_outside_range_disagrees(self):
        spec = _spec(pillar_ranges={"code_refinement": (5, 5)})
        failures = evaluate_agreement(spec, _run(scores={"code_refinement": 3}))
        assert any("outside expected" in f for f in failures)

    def test_wrong_classification_disagrees(self):
        failures = evaluate_agreement(_spec(), _run(classification="Copy-Paster", recommendation="Reject"))
        assert any("classification" in f for f in failures)
        assert any("recommendation" in f for f in failures)

    def test_missing_pillar_in_ranges_disagrees(self):
        spec = _spec(pillar_ranges={"documentation_quality": (3, 5)})
        failures = evaluate_agreement(spec, _run())
        assert any("missing" in f for f in failures)


class TestConsistency:
    def test_spread_and_stability(self):
        runs = [
            _run(scores={"architecture_awareness": 5, "edge_case_coverage": 5}),
            _run(scores={"architecture_awareness": 4, "edge_case_coverage": 5}),
        ]
        report = consistency_report(runs)
        assert report["pillar_spread"] == {"architecture_awareness": 1, "edge_case_coverage": 0}
        assert report["classification_stable"] is True
        assert report["findings"] == []

    def test_spread_over_one_is_a_finding_not_a_failure(self):
        runs = [
            _run(scores={"edge_case_coverage": 5}),
            _run(scores={"edge_case_coverage": 3}),
        ]
        report = consistency_report(runs)
        assert report["pillar_spread"]["edge_case_coverage"] == 2
        assert any("spread 2 > 1" in f for f in report["findings"])

    def test_unstable_classification_is_a_finding(self):
        runs = [_run(), _run(classification="Assisted Engineer")]
        report = consistency_report(runs)
        assert report["classification_stable"] is False
        assert any("unstable" in f for f in report["findings"])


class TestJudge:
    def _discrepancies(self):
        return [
            Discrepancy("error-handling-conflict", "s", "r", resolution="Scanner blind to Elixir."),
            Discrepancy("linter-conflict", "s", "r", resolution="Credo checks exist in code."),
        ]

    @patch("vetter.eval_harness.router.complete")
    def test_verdicts_enter_the_result(self, mock_complete):
        mock_complete.return_value = json.dumps({
            "error-handling-conflict": "specific",
            "linter-conflict": "generic",
        })
        verdicts = judge_resolutions(self._discrepancies())
        assert verdicts == {
            "error-handling-conflict": "specific",
            "linter-conflict": "generic",
        }
        # Bounded input: only rules + resolutions, never repo content
        user_content = mock_complete.call_args.kwargs["user_content"]
        assert "Scanner blind to Elixir." in user_content
        assert len(user_content) < 1500

    @patch("vetter.eval_harness.router.complete")
    def test_judge_failure_is_recorded_not_raised(self, mock_complete):
        mock_complete.return_value = "the resolutions look fine to me"
        verdicts = judge_resolutions(self._discrepancies())
        assert "judge_error" in verdicts

    @patch("vetter.eval_harness.router.complete")
    def test_judge_call_failure_is_recorded_not_raised(self, mock_complete):
        mock_complete.side_effect = click.ClickException("provider down")
        verdicts = judge_resolutions(self._discrepancies())
        assert "provider down" in verdicts["judge_error"]

    @patch("vetter.eval_harness.router.complete")
    def test_no_discrepancies_no_call(self, mock_complete):
        assert judge_resolutions([]) == {}
        mock_complete.assert_not_called()


class TestRunEvalRegression:
    """Eval 4: exit code != 0 on any classification disagreement."""

    def _golden_set(self, tmp_path, expected_classification="AI Orchestrator",
                    expected_recommendation="Pass"):
        repo_dir = tmp_path / "some-repo"
        repo_dir.mkdir()
        return _write_golden_set(tmp_path, [{
            "id": "some-repo",
            "path": str(repo_dir),
            "runs": 2,
            "expected": {
                "classification": expected_classification,
                "recommendation": expected_recommendation,
            },
        }])

    @patch("vetter.eval_harness.run_specimen_once")
    def test_agreement_exits_zero(self, mock_run, tmp_path):
        mock_run.return_value = {**_run(), "agreement_failures": []}
        gs = self._golden_set(tmp_path)

        results, exit_code = run_eval(gs, echo=lambda *_: None)

        assert exit_code == 0
        assert results["verdict"] == "AGREE"
        assert mock_run.call_count == 2  # honors runs=2

    @patch("vetter.eval_harness.run_specimen_once")
    def test_synthetic_disagreement_exits_nonzero(self, mock_run, tmp_path):
        # The specimen is labeled AI Orchestrator/Pass; the pipeline "returns"
        # Copy-Paster/Reject — the harness must scream.
        mock_run.return_value = _run(classification="Copy-Paster", recommendation="Reject")
        gs = self._golden_set(tmp_path)

        results, exit_code = run_eval(gs, echo=lambda *_: None)

        assert exit_code == 1
        assert results["verdict"] == "DISAGREE"
        specimen = results["specimens"][0]
        assert specimen["verdict"] == "DISAGREE"
        assert any("classification" in r for r in specimen["disagree_reasons"])


class TestEvalCLI:
    @patch("vetter.eval_harness.run_specimen_once")
    def test_eval_command_writes_json_and_propagates_exit_code(self, mock_run, tmp_path):
        mock_run.return_value = _run(classification="Copy-Paster", recommendation="Reject")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        gs = _write_golden_set(tmp_path, [{
            "id": "repo",
            "path": str(repo_dir),
            "expected": {"classification": "AI Orchestrator", "recommendation": "Pass"},
        }])
        out_dir = tmp_path / "results"

        runner = CliRunner()
        result = runner.invoke(main, [
            "eval", "--golden-set", gs, "--output-dir", str(out_dir),
        ])

        assert result.exit_code == 1
        written = list(out_dir.glob("run-*.json"))
        assert len(written) == 1
        payload = json.loads(written[0].read_text())
        assert payload["verdict"] == "DISAGREE"
        assert "DISAGREE" in result.output
