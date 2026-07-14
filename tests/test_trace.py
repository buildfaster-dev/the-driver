from unittest.mock import patch

from vetter.trace import StageTimer


class TestStageTimer:
    @patch("vetter.trace.time.monotonic")
    def test_records_each_stage_duration(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, 1.5, 10.0, 12.5]
        timer = StageTimer()

        with timer.stage("ingest"):
            pass
        with timer.stage("review"):
            pass

        assert timer.stages == {"ingest": 1.5, "review": 2.5}

    @patch("vetter.trace.time.monotonic")
    def test_repeated_stage_accumulates(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, 1.0, 2.0, 3.5]
        timer = StageTimer()

        with timer.stage("gate"):
            pass
        with timer.stage("gate"):
            pass

        assert timer.stages == {"gate": 2.5}

    @patch("vetter.trace.time.monotonic")
    def test_stage_recorded_even_if_body_raises(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, 4.0]
        timer = StageTimer()

        try:
            with timer.stage("review"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert timer.stages == {"review": 4.0}

    @patch("vetter.trace.time.monotonic")
    def test_summary_answers_where_time_went(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, 0.4, 1.0, 69.3, 70.0, 82.0]
        timer = StageTimer()

        with timer.stage("ingest"):
            pass
        with timer.stage("review"):
            pass
        with timer.stage("gate"):
            pass

        summary = timer.summary()
        assert summary == "Trace: ingest 0.4s | review 68.3s | gate 12.0s | total 80.7s"
