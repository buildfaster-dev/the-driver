from vetter.models import (
    RepoData, FileInfo, CommitInfo,
    ScanResult, ReviewResult, PillarScore, Discrepancy,
)
from vetter.report import generate_report, _classify


def _make_review(arch=4, refine=4, edge=4):
    return ReviewResult(
        pillar_scores=[
            PillarScore("architecture_awareness", "Architecture Awareness", arch, "Good architecture.", []),
            PillarScore("code_refinement", "Code Refinement", refine, "Clean code.", []),
            PillarScore("edge_case_coverage", "Edge Case Coverage", edge, "Good coverage.", []),
        ],
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


class TestDiscrepanciesSection:
    def test_section_always_present_and_says_none_when_empty(self):
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=_make_scan(),
            review_result=_make_review(),
        )
        assert "## Scan/Review Discrepancies" in report
        assert "None detected" in report

    def test_section_names_each_discrepancy_and_resolution(self):
        discrepancies = [
            Discrepancy(
                rule="error-handling-conflict",
                scan_says="error handling pattern MINIMAL",
                review_says="Edge Case Coverage 5/5: comprehensive handling",
                resolution="The scanner does not cover this ecosystem; code evidence stands.",
            ),
            Discrepancy(
                rule="dependencies-conflict",
                scan_says="no dependency manifest detected",
                review_says="Code Refinement 5/5: thoughtful libraries",
                resolution="Manifest format unknown to the scanner; libraries verified in code.",
            ),
        ]
        report = generate_report(
            repo_data=_make_repo_data(),
            scan_result=_make_scan(),
            review_result=_make_review(),
            discrepancies=discrepancies,
        )
        assert "## Scan/Review Discrepancies" in report
        assert "None detected" not in report
        for d in discrepancies:
            assert d.rule in report
            assert d.resolution in report
