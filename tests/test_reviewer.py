import json
import pytest
import click
from unittest.mock import patch, MagicMock
from vetter.models import RepoData, FileInfo
from vetter.reviewer import (
    review_repo,
    _parse_review_response,
    _build_codebase_context,
    _clamp_score,
    _validate_review_result,
)


VALID_RESPONSE = json.dumps({
    "architecture_awareness": {
        "score": 4,
        "justification": "Well-structured project.",
        "evidence": ["src/app.py:1 — good structure"],
    },
    "code_refinement": {
        "score": 3,
        "justification": "Reasonable code quality.",
        "evidence": ["src/app.py:10 — idiomatic loop"],
    },
    "edge_case_coverage": {
        "score": 2,
        "justification": "Minimal tests.",
        "evidence": ["tests/ — empty"],
    },
    "overall_summary": "Decent submission with room for improvement.",
})


def _make_repo():
    return RepoData(
        path="/fake/repo",
        files=[FileInfo("app.py", "print('hello')", "Python", 15, False)],
        commits=[],
        languages={"Python": 1},
        total_files=1,
        total_lines=1,
    )


class TestParseResponse:
    def test_valid_json(self):
        result = _parse_review_response(VALID_RESPONSE)
        assert result.architecture_awareness.score == 4
        assert result.code_refinement.score == 3
        assert result.edge_case_coverage.score == 2
        assert "Decent submission" in result.overall_summary

    def test_json_in_code_block(self):
        wrapped = f"```json\n{VALID_RESPONSE}\n```"
        result = _parse_review_response(wrapped)
        assert result.architecture_awareness.score == 4

    def test_invalid_json(self):
        with pytest.raises(click.ClickException, match="Failed to parse"):
            _parse_review_response("this is not json")

    def test_missing_field(self):
        incomplete = json.dumps({"architecture_awareness": {"score": 4}})
        with pytest.raises(click.ClickException, match="missing expected field"):
            _parse_review_response(incomplete)


class TestBuildContext:
    def test_includes_file_tree(self):
        repo = _make_repo()
        context = _build_codebase_context(repo)
        assert "app.py" in context
        assert "File Tree" in context

    def test_includes_source_content(self):
        repo = _make_repo()
        context = _build_codebase_context(repo)
        assert "print('hello')" in context


class TestReviewRepo:
    def test_missing_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(click.ClickException, match="ANTHROPIC_API_KEY"):
                review_repo(_make_repo())

    @patch("vetter.router.anthropic.Anthropic")
    def test_successful_review(self, mock_anthropic_class):
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=VALID_RESPONSE)]
        mock_message.usage.input_tokens = 1000
        mock_message.usage.output_tokens = 200
        mock_client.messages.create.return_value = mock_message

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = review_repo(_make_repo())

        assert result.architecture_awareness.score == 4
        assert result.code_refinement.score == 3
        assert result.edge_case_coverage.score == 2


class TestValidateReviewResult:
    def _result_from(self, overall_summary=None, **pillar_overrides):
        data = json.loads(VALID_RESPONSE)
        for pillar, fields in pillar_overrides.items():
            data[pillar].update(fields)
        if overall_summary is not None:
            data["overall_summary"] = overall_summary
        raw = json.dumps(data)
        return _parse_review_response(raw), raw

    def test_valid_result_passes(self):
        result, raw = self._result_from()
        _validate_review_result(result, raw)  # must not raise

    def test_empty_justification_rejected(self):
        result, raw = self._result_from(code_refinement={"justification": "   "})
        with pytest.raises(click.ClickException, match="empty justification"):
            _validate_review_result(result, raw)

    def test_empty_evidence_rejected(self):
        result, raw = self._result_from(edge_case_coverage={"evidence": []})
        with pytest.raises(click.ClickException, match="no evidence"):
            _validate_review_result(result, raw)

    def test_blank_evidence_entries_rejected(self):
        result, raw = self._result_from(architecture_awareness={"evidence": ["  ", ""]})
        with pytest.raises(click.ClickException, match="no evidence"):
            _validate_review_result(result, raw)

    def test_empty_overall_summary_rejected(self):
        result, raw = self._result_from(overall_summary="")
        with pytest.raises(click.ClickException, match="overall_summary"):
            _validate_review_result(result, raw)


class TestClampScore:
    def test_valid_score_unchanged(self):
        assert _clamp_score(3) == 3

    def test_zero_clamped_to_1(self):
        assert _clamp_score(0) == 1

    def test_negative_clamped_to_1(self):
        assert _clamp_score(-2) == 1

    def test_six_clamped_to_5(self):
        assert _clamp_score(6) == 5

    def test_ten_clamped_to_5(self):
        assert _clamp_score(10) == 5

    def test_float_rounded(self):
        assert _clamp_score(3.7) == 4

    def test_float_rounded_down(self):
        assert _clamp_score(2.3) == 2


class TestParseResponseScoreClamping:
    def test_out_of_range_scores_clamped(self):
        response = json.dumps({
            "architecture_awareness": {"score": 0, "justification": "test", "evidence": []},
            "code_refinement": {"score": 10, "justification": "test", "evidence": []},
            "edge_case_coverage": {"score": 3.7, "justification": "test", "evidence": []},
            "overall_summary": "test",
        })
        result = _parse_review_response(response)
        assert result.architecture_awareness.score == 1
        assert result.code_refinement.score == 5
        assert result.edge_case_coverage.score == 4
