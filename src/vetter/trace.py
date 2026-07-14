"""Per-stage wall-clock tracing for a single vetter run.

Operator telemetry: the summary goes to stderr at the end of a run, so the
question "where did the time go?" is answered by one line. Cost/token truth
stays in the router's JSONL.
"""

import time
from contextlib import contextmanager


class StageTimer:
    def __init__(self) -> None:
        self.stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            self.stages[name] = self.stages.get(name, 0.0) + elapsed

    def summary(self) -> str:
        total = sum(self.stages.values())
        parts = " | ".join(f"{name} {seconds:.1f}s" for name, seconds in self.stages.items())
        return f"Trace: {parts} | total {total:.1f}s"
