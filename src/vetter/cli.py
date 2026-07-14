import json
import os
from datetime import datetime, timezone

import click
from rich.console import Console

from vetter.ingester import ingest_repo
from vetter.scanner import scan_repo
from vetter.reviewer import review_repo
from vetter.gate import detect_discrepancies, resolve_discrepancies
from vetter.guardrails import detect_injection_attempts
from vetter.report import generate_report
from vetter.router import RunLimitExceeded
from vetter.trace import StageTimer

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Vetter — AI-powered code review for technical hiring."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--candidate", default=None, help="Candidate name (report header only, does not affect analysis).")
@click.option("--repo-url", default=None, help="Repository URL (report header only — does not clone).")
@click.option("--output", default="./report.md", help="Output file path (default: ./report.md).")
@click.option("--model", default="sonnet", help="Claude model: sonnet (default) or opus.")
def analyze(repo_path: str, candidate: str | None, repo_url: str | None, output: str, model: str):
    """Analyze a local Git repository and generate a report."""
    output_dir = os.path.dirname(os.path.abspath(output))
    if not os.path.isdir(output_dir):
        raise click.ClickException(f"Output directory does not exist: {output_dir}")

    timer = StageTimer()
    try:
        with console.status("[bold green]Ingesting repository..."), timer.stage("ingest"):
            repo_data = ingest_repo(repo_path)

        console.print(f"[green]✓[/green] Ingested {repo_data.total_files} files, {len(repo_data.commits)} commits")

        with console.status("[bold green]Running automated scan..."), timer.stage("scan"):
            scan_result = scan_repo(repo_data)

        console.print("[green]✓[/green] Automated scan complete")

        injection_attempts = detect_injection_attempts(repo_data)
        if injection_attempts:
            console.print(f"[yellow]![/yellow] Guardrail: {len(injection_attempts)} prompt-injection attempt(s) detected in submission")

        with console.status("[bold green]Running AI expert review..."), timer.stage("review"):
            review_result = review_repo(repo_data, scan_result, model=model)

        console.print("[green]✓[/green] AI review complete")

        with console.status("[bold green]Confronting scan vs review..."), timer.stage("gate"):
            discrepancies = detect_discrepancies(scan_result, review_result)
            if discrepancies:
                discrepancies = resolve_discrepancies(discrepancies, model=model)

        if discrepancies:
            console.print(f"[yellow]![/yellow] Gate: {len(discrepancies)} discrepancies confronted and resolved")
        else:
            console.print("[green]✓[/green] Gate: scan and review are consistent")

        with console.status("[bold green]Generating report..."), timer.stage("report"):
            report = generate_report(
                repo_data=repo_data,
                scan_result=scan_result,
                review_result=review_result,
                candidate=candidate,
                repo_url=repo_url,
                discrepancies=discrepancies,
                injection_attempts=injection_attempts,
            )

            with open(output, "w") as f:
                f.write(report)

        console.print(f"[green]✓[/green] Report saved to [bold]{output}[/bold]")
        click.echo(timer.summary(), err=True)
    except RunLimitExceeded as e:
        # Honest partial report: the run was cut, the candidate was not scored.
        partial = (
            "# Candidate Assessment Report — INCOMPLETE\n\n"
            f"**Analysis stopped before completion: {e}**\n\n"
            "The per-run guardrail cut this analysis to avoid a runaway cost or "
            "an indefinite hang. The candidate was **not scored**; no "
            "classification or recommendation was produced. Re-run with a higher "
            "budget if this repository legitimately requires more analysis.\n"
        )
        with open(output, "w") as f:
            f.write(partial)
        console.print(f"[red]✗[/red] Run cut by guardrail: {e}")
        console.print(f"[yellow]![/yellow] Partial report saved to [bold]{output}[/bold]")
        click.echo(timer.summary(), err=True)
        raise SystemExit(2)
    except click.ClickException:
        raise
    except FileNotFoundError as e:
        raise click.ClickException(f"File not found: {e}")
    except PermissionError as e:
        raise click.ClickException(f"Permission denied: {e}")
    except Exception as e:
        raise click.ClickException(f"Unexpected error: {e}")


@main.command("eval")
@click.option("--golden-set", "golden_set_path", default="./golden_set.json",
              help="Path to the golden set JSON (default: ./golden_set.json).")
@click.option("--model", default="sonnet", help="Claude model alias (default: sonnet).")
@click.option("--output-dir", default="./eval-results",
              help="Directory for machine-readable results (default: ./eval-results).")
def eval_cmd(golden_set_path: str, model: str, output_dir: str):
    """Run the golden-set evaluation harness and report agreement.

    Exits non-zero when any specimen's classification disagrees with its
    human label (regression signal).
    """
    from vetter.eval_harness import format_summary, run_eval

    results, exit_code = run_eval(golden_set_path, model=model, echo=console.print)

    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    output_path = os.path.join(output_dir, f"run-{stamp}.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    console.print(format_summary(results))
    console.print(f"Machine-readable results: [bold]{output_path}[/bold]")
    if exit_code != 0:
        raise SystemExit(exit_code)
