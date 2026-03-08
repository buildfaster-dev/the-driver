import os
import click
from rich.console import Console

from vetter.ingester import ingest_repo
from vetter.scanner import scan_repo
from vetter.reviewer import review_repo
from vetter.report import generate_report

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

    try:
        with console.status("[bold green]Ingesting repository..."):
            repo_data = ingest_repo(repo_path)

        console.print(f"[green]✓[/green] Ingested {repo_data.total_files} files, {len(repo_data.commits)} commits")

        with console.status("[bold green]Running automated scan..."):
            scan_result = scan_repo(repo_data)

        console.print("[green]✓[/green] Automated scan complete")

        with console.status("[bold green]Running AI expert review..."):
            review_result = review_repo(repo_data, model=model)

        console.print("[green]✓[/green] AI review complete")

        with console.status("[bold green]Generating report..."):
            report = generate_report(
                repo_data=repo_data,
                scan_result=scan_result,
                review_result=review_result,
                candidate=candidate,
                repo_url=repo_url,
            )

        with open(output, "w") as f:
            f.write(report)

        console.print(f"[green]✓[/green] Report saved to [bold]{output}[/bold]")
    except click.ClickException:
        raise
    except FileNotFoundError as e:
        raise click.ClickException(f"File not found: {e}")
    except PermissionError as e:
        raise click.ClickException(f"Permission denied: {e}")
    except Exception as e:
        raise click.ClickException(f"Unexpected error: {e}")
