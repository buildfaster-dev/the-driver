import os
from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader
from the_driver.models import RepoData, ScanResult, ReviewResult, Classification


def _classify(review_result: ReviewResult) -> Classification:
    scores = [
        review_result.architecture_awareness.score,
        review_result.code_refinement.score,
        review_result.edge_case_coverage.score,
    ]
    avg = sum(scores) / len(scores)

    if avg >= 4:
        label = "AI Orchestrator"
        recommendation = "Pass"
    elif avg >= 3:
        label = "Assisted Engineer"
        recommendation = "Review Further"
    else:
        label = "Copy-Paster"
        recommendation = "Reject"

    return Classification(label=label, recommendation=recommendation, average_score=avg)


def generate_report(
    repo_data: RepoData,
    scan_result: ScanResult,
    review_result: ReviewResult,
    candidate: str | None = None,
    repo_url: str | None = None,
) -> str:
    classification = _classify(review_result)

    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template("report.md.j2")

    source_files = [f for f in repo_data.files if not f.is_test and f.language not in ("Markdown", "JSON", "YAML", "TOML")]
    test_files = [f for f in repo_data.files if f.is_test]

    return template.render(
        candidate=candidate,
        repo_url=repo_url,
        repo_data=repo_data,
        scan_result=scan_result,
        review_result=review_result,
        classification=classification,
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        source_file_count=len(source_files),
        test_file_count=len(test_files),
    )
