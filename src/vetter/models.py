from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FileInfo:
    path: str
    content: str
    language: str
    size: int
    is_test: bool


@dataclass
class CommitInfo:
    hash: str
    message: str
    author: str
    date: datetime
    files_changed: int
    insertions: int
    deletions: int


@dataclass
class RepoData:
    path: str
    files: list[FileInfo]
    commits: list[CommitInfo]
    languages: dict[str, int]
    total_files: int
    total_lines: int


@dataclass
class ScanResult:
    test_ratio: float
    has_linter_config: bool
    linter_configs_found: list[str]
    commit_count: int
    commit_quality: str  # "good" / "fair" / "poor"
    commit_messages: list[str]
    dependencies: list[str]
    error_handling: str  # "strategic" / "blanket" / "minimal"
    security_flags: list[str]
    languages: dict[str, int]


@dataclass
class PillarScore:
    name: str
    score: int  # 1-5
    justification: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    architecture_awareness: PillarScore
    code_refinement: PillarScore
    edge_case_coverage: PillarScore
    overall_summary: str


@dataclass
class Discrepancy:
    """A scan-vs-review contradiction caught by the gate, plus its resolution."""

    rule: str  # machine id of the gate rule that fired
    scan_says: str
    review_says: str
    resolution: str | None = None  # reviewer's explicit resolution (second call)


@dataclass
class Classification:
    label: str  # "Copy-Paster" / "Assisted Engineer" / "AI Orchestrator"
    recommendation: str  # "Pass" / "Review Further" / "Reject"
    average_score: float
