import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from vetter.cli import main


VALID_RESPONSE = json.dumps({
    "architecture_awareness": {
        "score": 4,
        "justification": "Well-structured project.",
        "evidence": ["src/app.py:1 — good structure"],
    },
    "code_refinement": {
        "score": 3,
        "justification": "Reasonable code quality.",
        "evidence": [],
    },
    "edge_case_coverage": {
        "score": 2,
        "justification": "Minimal tests.",
        "evidence": ["tests/ — empty"],
    },
    "overall_summary": "Decent submission with room for improvement.",
})


class TestCLIHelp:
    def test_main_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Vetter" in result.output

    def test_analyze_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["analyze", "--help"])
        assert result.exit_code == 0
        assert "REPO_PATH" in result.output
        assert "--candidate" in result.output
        assert "--repo-url" in result.output


class TestCLIErrors:
    def test_nonexistent_path(self):
        runner = CliRunner()
        result = runner.invoke(main, ["analyze", "/nonexistent/path"])
        assert result.exit_code != 0

    def test_non_git_directory(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(main, ["analyze", tmpdir])
        assert result.exit_code != 0
        assert "Not a Git repository" in result.output

    def test_bad_output_directory(self):
        runner = CliRunner()
        # Use the actual vetter-cli repo (cwd is a git repo)
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = runner.invoke(main, [
            "analyze", repo_path,
            "--output", "/nonexistent/dir/report.md",
        ])
        assert result.exit_code != 0
        assert "Output directory does not exist" in result.output

    def test_missing_api_key(self):
        runner = CliRunner()
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(main, ["analyze", repo_path])
        assert result.exit_code != 0
        assert "ANTHROPIC_API_KEY" in result.output


class TestCLISuccess:
    @patch("vetter.cli.review_repo")
    def test_successful_run(self, mock_review):
        from vetter.reviewer import _parse_review_response
        mock_review.return_value = _parse_review_response(VALID_RESPONSE)

        runner = CliRunner()
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "report.md")
            result = runner.invoke(main, [
                "analyze", repo_path,
                "--output", output_path,
                "--candidate", "Test Candidate",
            ])
            assert result.exit_code == 0, f"Failed with: {result.output}"
            assert os.path.exists(output_path)
            with open(output_path) as f:
                report = f.read()
            assert "Test Candidate" in report
            assert "Architecture Awareness" in report
