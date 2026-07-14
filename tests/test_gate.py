import json
from unittest.mock import patch

import click
import pytest

from vetter.gate import (
    SECURITY_FLAGS_THRESHOLD,
    STRONG_SCORE,
    detect_discrepancies,
    resolve_discrepancies,
)
from vetter.models import Discrepancy, PillarScore, ReviewResult, ScanResult

# Content of a hypothetical repo file. It informs the review text below only
# conceptually — the bounded-resolution test asserts this never reaches the
# resolution call.
REPO_FILE_SENTINEL = "SENTINEL_SOURCE_LINE = 'must_never_reach_the_resolution_call'"


def _scan(**overrides):
    defaults = dict(
        test_ratio=0.55,
        has_linter_config=False,
        linter_configs_found=[],
        commit_count=120,
        commit_quality="good",
        commit_messages=["feat: x"],
        dependencies=[],
        error_handling="minimal",
        security_flags=[],
        languages={"Other": 180},
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


def _review(arch=5, refine=5, edge=5, arch_just="Clean layering.",
            refine_just="Idiomatic code.", edge_just="Comprehensive handling."):
    return ReviewResult(
        architecture_awareness=PillarScore("Architecture Awareness", arch, arch_just, []),
        code_refinement=PillarScore("Code Refinement", refine, refine_just, []),
        edge_case_coverage=PillarScore("Edge Case Coverage", edge, edge_just, []),
        overall_summary="Overall strong.",
    )


class TestDetectDiscrepancies:
    """Eval 2: the three phase-00 contradictions, reproduced synthetically."""

    def test_error_handling_conflict_fires(self):
        # Phase 00 #1: scanner says MINIMAL, reviewer scores Edge Cases 5/5
        found = detect_discrepancies(
            _scan(error_handling="minimal"),
            _review(edge=5, edge_just="Comprehensive error handling: circuit breaker, DLQ idempotency."),
        )
        assert "error-handling-conflict" in [d.rule for d in found]

    def test_linter_conflict_fires_on_lint_mention(self):
        # Phase 00 #2: scanner finds no linter config, review praises lint-time checks
        found = detect_discrepancies(
            _scan(has_linter_config=False),
            _review(arch_just="Custom Credo checks enforce the architecture at lint time."),
        )
        conflict = next(d for d in found if d.rule == "linter-conflict")
        assert "lint" in conflict.review_says.lower()

    def test_dependencies_conflict_fires(self):
        # Phase 00 #3: no dependency manifest detected, refinement (library choices) scored high
        found = detect_discrepancies(
            _scan(dependencies=[]),
            _review(refine=4, refine_just="Thoughtful library choices: Cachex, Hammer, Cloak."),
        )
        assert "dependencies-conflict" in [d.rule for d in found]

    def test_security_flags_rule_respects_threshold(self):
        flags = [f"file{i}.txt: potential hardcoded secret detected" for i in range(11)]
        fired = detect_discrepancies(_scan(security_flags=flags), _review(edge=5))
        assert "security-flags-unaddressed" in [d.rule for d in fired]

        below = flags[: SECURITY_FLAGS_THRESHOLD - 1]
        not_fired = detect_discrepancies(_scan(security_flags=below), _review(edge=5))
        assert "security-flags-unaddressed" not in [d.rule for d in not_fired]

    def test_coherent_poor_repo_triggers_nothing(self):
        # Poor scan + low scores + no lint talk = consistent, no false positives
        found = detect_discrepancies(
            _scan(error_handling="minimal", dependencies=[], has_linter_config=False),
            _review(
                arch=2, refine=2, edge=2,
                arch_just="Everything in one file.",
                refine_just="Raw boilerplate.",
                edge_just="No tests, no validation.",
            ),
        )
        assert found == []

    def test_coherent_strong_repo_triggers_nothing(self):
        found = detect_discrepancies(
            _scan(
                error_handling="strategic",
                has_linter_config=True,
                dependencies=["pip: requirements.txt"],
            ),
            _review(arch=5, refine=5, edge=5),
        )
        assert found == []

    def test_thresholds_are_the_named_constants(self):
        # score below STRONG_SCORE never fires the score-based rules
        found = detect_discrepancies(
            _scan(error_handling="minimal", dependencies=[]),
            _review(refine=STRONG_SCORE - 1, edge=STRONG_SCORE - 1),
        )
        assert found == []


def _resolutions_for(discrepancies):
    return json.dumps({d.rule: f"Resolved: scanner blind spot for {d.rule}." for d in discrepancies})


class TestResolveDiscrepancies:
    def _detected(self):
        return detect_discrepancies(
            _scan(error_handling="minimal", security_flags=["a: secret", "b: secret", "c: secret"]),
            _review(edge=5, edge_just="Comprehensive handling verified in tests."),
        )

    @patch("vetter.gate.router.complete")
    def test_attaches_resolution_per_rule(self, mock_complete):
        discrepancies = self._detected()
        mock_complete.return_value = _resolutions_for(discrepancies)

        resolved = resolve_discrepancies(discrepancies)

        assert mock_complete.call_count == 1
        assert all(d.resolution and d.rule in d.resolution for d in resolved)

    @patch("vetter.gate.router.complete")
    def test_resolution_call_is_bounded_no_repo_content(self, mock_complete):
        # The mandated filter: the second call carries ONLY the discrepancies —
        # scan fields + implicated review justifications. Never file contents.
        discrepancies = self._detected()
        mock_complete.return_value = _resolutions_for(discrepancies)

        resolve_discrepancies(discrepancies)

        user_content = mock_complete.call_args.kwargs["user_content"]
        assert REPO_FILE_SENTINEL not in user_content
        for d in discrepancies:
            assert d.rule in user_content
            assert d.scan_says in user_content
        # Hundreds of tokens, not thousands: chars are a hard proxy
        assert len(user_content) < 2000

    @patch("vetter.gate.router.complete")
    def test_code_fenced_json_is_accepted(self, mock_complete):
        discrepancies = self._detected()
        mock_complete.return_value = f"```json\n{_resolutions_for(discrepancies)}\n```"
        resolved = resolve_discrepancies(discrepancies)
        assert all(d.resolution for d in resolved)

    @patch("vetter.gate.router.complete")
    def test_malformed_json_fails_honestly(self, mock_complete):
        discrepancies = self._detected()
        mock_complete.return_value = "I think the scanner is wrong about everything."
        with pytest.raises(click.ClickException, match="Failed to parse gate resolution"):
            resolve_discrepancies(discrepancies)

    @patch("vetter.gate.router.complete")
    def test_missing_rule_resolution_fails_honestly(self, mock_complete):
        discrepancies = self._detected()
        mock_complete.return_value = json.dumps({discrepancies[0].rule: "Only this one."})
        with pytest.raises(click.ClickException, match="resolution missing"):
            resolve_discrepancies(discrepancies)

    @patch("vetter.gate.router.complete")
    def test_no_discrepancies_makes_no_model_call(self, mock_complete):
        assert resolve_discrepancies([]) == []
        mock_complete.assert_not_called()
