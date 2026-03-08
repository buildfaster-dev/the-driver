from the_driver.models import (
    RepoData, FileInfo, CommitInfo,
    ScanResult, ReviewResult, PillarScore,
)
from the_driver.report import generate_report, _classify


def _make_review(arch=4, refine=4, edge=4):
    return ReviewResult(
        architecture_awareness=PillarScore("Architecture Awareness", arch, "Good architecture.", []),
        code_refinement=PillarScore("Code Refinement", refine, "Clean code.", []),
        edge_case_coverage=PillarScore("Edge Case Coverage", edge, "Good coverage.", []),
        overall_summary="Solid submission overall.",
    )


def _make_scan():
    return ScanResult(
        test_ratio=0.5,
        has_linter_config=True,
        linter_configs_found=["pyproject.toml"],
        commit_count=10,
        commit_quality="good",
        commit_messages=["feat: add auth", "fix: handle null"],
        dependencies=["uv/pip: pyproject.toml"],
        error_handling="strategic",
        security_flags=[],
        languages={"Python": 5},
    )


def _make_repo_data():
    return RepoData(
        path="/fake/repo",
        files=[FileInfo("app.py", "print('hi')", "Python", 11, False)],
        commits=[],
        languages={"Python": 1},
        total_files=1,
        total_lines=1,
    )


class TestClassification:
    def test_ai_orchestrator(self):
        result = _classify(_make_review(5, 4, 4))
        assert result.label == "AI Orchestrator"
        assert result.recommendation == "Pass"

    def test_assisted_engineer(self):
        result = _classify(_make_review(3, 3, 3))
        assert result.label == "Assisted Engineer"
        assert result.recommendation == "Review Further"

    def test_copy_paster(self):
        result = _classify(_make_review(1, 2, 2))
        assert result.label == "Copy-Paster"
        assert result.recommendation == "Reject"

    def test_boundary_at_4(self):
        result = _classify(_make_review(4, 4, 4))
        assert result.label == "AI Orchestrator"

    def test_boundary_at_3(self):
        result = _classify(_make_review(3, 3, 2))
        assert result.label == "Copy-Paster"


class TestReportGeneration:
    def test_generates_markdown(self):
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=_make_scan(),
            review_result=_make_review(),
            candidate="John Doe",
            repo_url="https://github.com/test/repo",
        )
        assert "# Candidate Assessment Report" in report
        assert "John Doe" in report
        assert "https://github.com/test/repo" in report
        assert "AI Orchestrator" in report
        assert "Pass" in report

    def test_default_candidate(self):
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=_make_scan(),
            review_result=_make_review(),
        )
        assert "Not specified" in report

    def test_includes_pillar_scores(self):
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=_make_scan(),
            review_result=_make_review(3, 4, 5),
        )
        assert "3/5" in report
        assert "4/5" in report
        assert "5/5" in report

    def test_low_commit_warning(self):
        scan = _make_scan()
        scan.commit_count = 1
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=scan,
            review_result=_make_review(),
        )
        assert "Warning" in report
