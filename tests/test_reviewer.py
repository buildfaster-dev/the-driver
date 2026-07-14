import json
import pytest
import click
from unittest.mock import patch, MagicMock
from vetter.models import RepoData, FileInfo, ScanResult
from vetter.reviewer import (
    review_repo,
    _parse_review_response,
    _build_codebase_context,
    _clamp_score,
    _render_file_block,
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


def _make_scan(**overrides):
    defaults = dict(
        test_ratio=0.0,
        has_linter_config=False,
        linter_configs_found=[],
        commit_count=3,
        commit_quality="poor",
        commit_messages=["init", "wip", "fix"],
        dependencies=[],
        error_handling="minimal",
        security_flags=["config.py: potential hardcoded secret detected"],
        languages={"Python": 1},
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


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


def _src(path, content, language="Python", is_test=False):
    return FileInfo(path, content, language, len(content), is_test)


def _repo_with(files):
    return RepoData(
        path="/fake/repo", files=files, commits=[],
        languages={"Python": len(files)}, total_files=len(files),
        total_lines=sum(len(f.content.splitlines()) for f in files),
    )


def _omitted_section(context):
    marker = "## Files Omitted from Context"
    assert marker in context, "omitted files were not announced"
    return context.split(marker, 1)[1]


class TestContextSelection:
    """Effects only: which file contents made it into the context, which didn't."""

    def test_entry_point_without_refs_beats_mid_fan_in_hub(self):
        # The weight conflict, pinned: ENTRY_POINT_BONUS (6) > FAN_IN_CAP (5),
        # so an unreferenced entry point must win over a mid-fan-in hub.
        entry = _src("main.py", "print('the entry point')")
        hub = _src("helpers.py", "def shared_helper(): return 42")
        refs = [_src(f"caller{i}.py", "import helpers\n") for i in range(3)]
        repo = _repo_with([entry, hub, *refs])

        budget = len(_render_file_block(entry))  # room for exactly one block
        context = _build_codebase_context(repo, char_budget=budget)

        assert "print('the entry point')" in context
        assert "def shared_helper" not in context
        omitted = _omitted_section(context)
        assert "helpers.py" in omitted

    def test_small_hub_survives_giant_low_signal_file(self):
        giant = _src("generated.py", "GENERATED_BLOB = 1\n" * 5000)
        core = _src("src/core.py", "class Core:\n    pass")
        refs = [_src(f"src/mod{i}.py", "from core import Core\n") for i in range(3)]
        repo = _repo_with([giant, core, *refs])

        context = _build_codebase_context(repo, char_budget=500)

        assert "class Core" in context
        assert "GENERATED_BLOB" not in context
        assert "generated.py" in _omitted_section(context)

    def test_fan_in_matches_pascal_case_references(self):
        # Elixir-style: file circuit_breaker.ex, module CircuitBreaker.
        breaker = _src("lib/circuit_breaker.ex", "defmodule CircuitBreaker do\nend", language="Other")
        orphan = _src("lib/orphan_module.ex", "defmodule OrphanModule do\nend", language="Other")
        refs = [
            _src(f"lib/user{i}.ex", "alias MyApp.CircuitBreaker\n", language="Other")
            for i in range(3)
        ]
        repo = _repo_with([breaker, orphan, *refs])

        budget = len(_render_file_block(breaker))  # one block only
        context = _build_codebase_context(repo, char_budget=budget)

        assert "defmodule CircuitBreaker" in context
        assert "defmodule OrphanModule" not in context
        assert "orphan_module.ex" in _omitted_section(context)

    def test_every_file_is_either_included_or_announced(self):
        files = [
            _src("alpha.py", "ALPHA_CONTENT = 1"),
            _src("beta.py", "BETA_CONTENT = 2"),
            _src("gamma.py", "GAMMA_CONTENT = 3"),
            _src("test_alpha.py", "TEST_ALPHA_CONTENT = 4", is_test=True),
        ]
        repo = _repo_with(files)

        context = _build_codebase_context(repo, char_budget=10)  # nothing fits

        omitted = _omitted_section(context)
        for f in files:
            assert f.content not in context
            assert f.path in omitted

    def test_rendered_blocks_are_charged_against_budget(self):
        # Headers and fences count too: the sum of included rendered blocks
        # never exceeds the budget.
        files = [_src(f"file{i}.py", f"CONTENT_{i} = {i}" * 10) for i in range(8)]
        repo = _repo_with(files)
        budget = 300

        context = _build_codebase_context(repo, char_budget=budget)

        included = [f for f in files if f.content in context]
        excluded = [f for f in files if f.content not in context]
        assert included, "budget should fit at least one file"
        assert excluded, "budget should force at least one exclusion"
        assert sum(len(_render_file_block(f)) for f in included) <= budget
        omitted = _omitted_section(context)
        for f in excluded:
            assert f.path in omitted

    def test_selection_is_deterministic(self):
        files = [
            _src("main.py", "entry"),
            _src("zeta.py", "z" * 200),
            _src("alpha.py", "a" * 200),
        ]
        repo = _repo_with(files)
        assert _build_codebase_context(repo, char_budget=250) == _build_codebase_context(
            repo, char_budget=250
        )


def _final_review_message(text=VALID_RESPONSE):
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.stop_reason = "end_turn"
    msg.content = [block]
    msg.usage.input_tokens = 1000
    msg.usage.output_tokens = 200
    return msg


def _tool_call_message(name, tool_id="tu_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = {}
    msg = MagicMock()
    msg.stop_reason = "tool_use"
    msg.content = [block]
    msg.usage.input_tokens = 1000
    msg.usage.output_tokens = 50
    return msg


class TestReviewRepo:
    def test_missing_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(click.ClickException, match="ANTHROPIC_API_KEY"):
                review_repo(_make_repo(), _make_scan())

    @patch("vetter.router.anthropic.Anthropic")
    def test_successful_review(self, mock_anthropic_class):
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = _final_review_message()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = review_repo(_make_repo(), _make_scan())

        assert result.architecture_awareness.score == 4
        assert result.code_refinement.score == 3
        assert result.edge_case_coverage.score == 2

    @patch("vetter.router.anthropic.Anthropic")
    def test_scan_tools_are_offered_to_the_model(self, mock_anthropic_class):
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = _final_review_message()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            review_repo(_make_repo(), _make_scan())

        offered = mock_client.messages.create.call_args.kwargs["tools"]
        assert {t["name"] for t in offered} == {
            "get_scan_summary", "get_security_flags", "get_test_metrics",
        }

    @patch("vetter.router.anthropic.Anthropic")
    def test_tool_result_is_the_real_scan_not_an_invention(self, mock_anthropic_class):
        # Eval 1 anti-invention clause: the payload the model receives must be
        # byte-derived from the real ScanResult.
        scan = _make_scan(security_flags=[
            "config/runtime.exs: potential hardcoded secret detected",
            "k8s/secret.yaml: potential hardcoded secret detected",
        ])
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _tool_call_message("get_security_flags", tool_id="tu_9"),
            _final_review_message(),
        ]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            review_repo(_make_repo(), scan)

        second_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result = second_messages[2]["content"][0]
        assert tool_result["tool_use_id"] == "tu_9"
        assert json.loads(tool_result["content"]) == {"security_flags": scan.security_flags}


class TestScanToolHandler:
    def test_scan_summary_matches_dataclass_fields(self):
        from vetter.reviewer import _scan_tool_handler
        scan = _make_scan(error_handling="strategic", dependencies=["pip: requirements.txt"])
        handler = _scan_tool_handler(scan, _make_repo())

        data = json.loads(handler("get_scan_summary", {}))

        assert data["error_handling"] == "strategic"
        assert data["dependencies"] == ["pip: requirements.txt"]
        assert data["test_ratio"] == scan.test_ratio
        assert data["has_linter_config"] == scan.has_linter_config
        assert "security_flags" not in data  # has its own tool

    def test_test_metrics_counts_real_files(self):
        from vetter.reviewer import _scan_tool_handler
        repo = RepoData(
            path="/fake",
            files=[
                FileInfo("app.py", "x", "Python", 1, False),
                FileInfo("notes.md", "x", "Markdown", 1, False),
                FileInfo("test_app.py", "x", "Python", 1, True),
            ],
            commits=[], languages={"Python": 2}, total_files=3, total_lines=3,
        )
        handler = _scan_tool_handler(_make_scan(test_ratio=1.0), repo)

        data = json.loads(handler("get_test_metrics", {}))

        assert data == {"test_ratio": 1.0, "source_file_count": 1, "test_file_count": 1}

    def test_unknown_tool_returns_error_payload(self):
        from vetter.reviewer import _scan_tool_handler
        handler = _scan_tool_handler(_make_scan(), _make_repo())
        assert "unknown tool" in json.loads(handler("get_weather", {}))["error"]


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
