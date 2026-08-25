"""Command-line interface for AgentCompat CI."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from agentcompat import __version__
from agentcompat.adapters import AgentAdapter, CodexAdapter, GeminiAdapter
from agentcompat.contracts import ContractValidationError, load_contract
from agentcompat.models import (
    AgentContract,
    AgentRunResult,
    CompatibilityResult,
    EvaluationResult,
)
from agentcompat.runner import (
    RunnerConfigurationError,
    TaskValidationError,
    run_compatibility,
)

app = typer.Typer(
    help="Test behavioural compatibility between AI coding agents.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    """Print the package version for the eager ``--version`` option."""
    if value:
        typer.echo(f"agentcompat {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            help="Show the version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """AgentCompat CI command-line interface."""


def _format_items(items: list[str]) -> str:
    """Format a contract list for compact terminal display."""
    return ", ".join(items) if items else "None"


def _show_contract_summary(contract_path: Path, contract: AgentContract) -> None:
    """Render the validated contract as a Rich summary table."""
    table = Table(title="Agent compatibility contract", show_header=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    table.add_row("File", str(contract_path))
    table.add_row("Version", str(contract.version))
    table.add_row("Project", contract.project.name)
    table.add_row("Baseline", contract.baseline.agent)
    table.add_row("Candidates", _format_items(contract.candidates))
    table.add_row("Tasks", _format_items(contract.tasks))
    table.add_row("Tests must pass", "Yes" if contract.rules.tests_must_pass else "No")
    table.add_row("Build must pass", "Yes" if contract.rules.build_must_pass else "No")
    table.add_row("Forbidden paths", _format_items(contract.rules.forbidden_paths))
    table.add_row("Required paths", _format_items(contract.rules.required_paths))
    table.add_row(
        "Max changed files",
        str(contract.rules.max_changed_files)
        if contract.rules.max_changed_files is not None
        else "Not set",
    )
    table.add_row(
        "Forbidden dependencies",
        _format_items(contract.rules.forbidden_dependencies),
    )

    console.print("[bold green]Contract is valid[/bold green]")
    console.print(table)


def _create_adapter(agent_name: str) -> AgentAdapter:
    """Create one of the agent adapters supported by the MVP CLI."""
    if agent_name == "codex":
        return CodexAdapter()
    if agent_name == "gemini":
        return GeminiAdapter()
    raise RunnerConfigurationError(f"unsupported agent: {agent_name}")


def _find_evaluation(
    result: CompatibilityResult,
    name: str,
) -> EvaluationResult | None:
    """Find one named evaluation in a compatibility report."""
    return next(
        (finding for finding in result.verdict.results if finding.name == name),
        None,
    )


def _evaluation_status(finding: EvaluationResult | None) -> str:
    """Format a deterministic check status for terminal output."""
    if finding is None:
        return "UNKNOWN"
    if finding.metadata.get("required") is False:
        return "NOT REQUIRED"
    return "PASS" if finding.passed else "FAIL"


def _changed_files(run: AgentRunResult) -> str:
    """Format modified, added, and deleted paths for one run."""
    changes = [
        *(f"modified: {path}" for path in run.diff.changed_files),
        *(f"added: {path}" for path in run.diff.added_files),
        *(f"deleted: {path}" for path in run.diff.deleted_files),
    ]
    return "\n".join(changes) if changes else "None"


def _execution_status(run: AgentRunResult) -> str:
    """Format an agent exit and a bounded failure diagnostic."""
    if run.error is not None:
        return f"ERROR — {run.error}"
    if run.execution is None:
        return "ERROR — no execution result"
    if run.execution.exit_code == 0:
        return "PASS (exit 0)"

    diagnostic = run.execution.stderr.strip() or run.execution.stdout.strip()
    if len(diagnostic) > 1_000:
        diagnostic = f"{diagnostic[:1_000]}…"
    status = f"FAIL (exit {run.execution.exit_code})"
    return f"{status}\n{diagnostic}" if diagnostic else status


def _task_label(result: CompatibilityResult) -> str:
    """Prefer a task name when supplied, falling back to its prompt."""
    task_name = (result.task.model_extra or {}).get("name")
    return task_name if isinstance(task_name, str) else result.task.prompt


def _critical_failures(result: CompatibilityResult) -> list[EvaluationResult]:
    """Return every failed blocking finding."""
    return [
        finding
        for finding in result.verdict.results
        if finding.blocking and not finding.passed
    ]


def _compatibility_percentage(result: CompatibilityResult) -> float:
    """Score configured deterministic candidate checks as a percentage."""
    configured_checks = [
        finding
        for finding in result.verdict.results
        if finding.blocking and not finding.name.endswith("_execution")
    ]
    if not configured_checks:
        return 100.0
    passed = sum(finding.passed for finding in configured_checks)
    return passed / len(configured_checks) * 100.0


def _required_path_status(finding: EvaluationResult | None) -> str:
    """Format required-path compliance and missing patterns."""
    if finding is None:
        return "UNKNOWN"
    if finding.passed:
        return "PASS"
    missing = finding.metadata.get("missing_patterns")
    if isinstance(missing, list) and missing:
        return f"FAIL — missing: {_format_items(missing)}"
    return "FAIL"


def _show_compatibility_report(result: CompatibilityResult) -> None:
    """Render a complete Rich terminal compatibility report."""
    forbidden = _find_evaluation(result, "forbidden_paths")
    required = _find_evaluation(result, "required_paths")
    critical = _critical_failures(result)
    final_status = "PASS" if result.passed else "FAIL"
    final_markup = (
        "[bold green]PASS[/bold green]"
        if result.passed
        else "[bold red]FAIL[/bold red]"
    )

    table = Table(title="AgentCompat CI compatibility report", show_header=False)
    table.add_column("Field", style="bold cyan", no_wrap=True)
    table.add_column("Result")
    table.add_row("Baseline", result.baseline.agent_name)
    table.add_row("Candidate", result.candidate.agent_name)
    table.add_row("Task", _task_label(result))
    table.add_row("Baseline execution", _execution_status(result.baseline))
    table.add_row("Candidate execution", _execution_status(result.candidate))
    table.add_row(
        "Build status",
        _evaluation_status(_find_evaluation(result, "build_must_pass")),
    )
    table.add_row(
        "Test status",
        _evaluation_status(_find_evaluation(result, "tests_must_pass")),
    )
    table.add_row("Baseline changed files", _changed_files(result.baseline))
    table.add_row("Candidate changed files", _changed_files(result.candidate))
    table.add_row(
        "Forbidden path violations",
        _format_items(forbidden.evidence) if forbidden is not None else "UNKNOWN",
    )
    table.add_row("Required path compliance", _required_path_status(required))
    table.add_row(
        "Compatibility percentage",
        f"{_compatibility_percentage(result):.1f}%",
    )
    table.add_row(
        "Critical failures",
        "\n".join(
            f"{finding.name}: {finding.summary}" for finding in critical
        )
        if critical
        else "None",
    )
    table.add_row("Final", final_markup)
    console.print(table)
    console.print(f"Final result: {final_markup} ({final_status})")


def _json_payload(result: CompatibilityResult) -> dict[str, object]:
    """Build the persisted JSON report payload."""
    payload: dict[str, object] = result.model_dump(mode="json")
    payload["compatibility_percentage"] = _compatibility_percentage(result)
    payload["critical_failures"] = [
        finding.model_dump(mode="json") for finding in _critical_failures(result)
    ]
    payload["final_status"] = "PASS" if result.passed else "FAIL"
    return payload


def _write_json_report(path: Path, result: CompatibilityResult) -> None:
    """Write the structured compatibility report as UTF-8 JSON."""
    path.write_text(
        f"{json.dumps(_json_payload(result), indent=2)}\n",
        encoding="utf-8",
    )


@app.command()
def validate(
    contract: Annotated[
        Path,
        typer.Argument(
            help="Path to an engineering contract YAML file.",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Load and validate an AgentCompat CI engineering contract."""
    try:
        loaded_contract = load_contract(contract)
    except ContractValidationError as exc:
        typer.secho(f"Invalid contract: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _show_contract_summary(contract, loaded_contract)


@app.command()
def run(
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            help="Source Git repository to test.",
            exists=True,
            file_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    baseline: Annotated[
        str,
        typer.Option("--baseline", help="Baseline agent name."),
    ],
    candidate: Annotated[
        str,
        typer.Option("--candidate", help="Candidate agent name."),
    ],
    contract: Annotated[
        Path,
        typer.Option(
            "--contract",
            help="Engineering contract YAML file.",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    task: Annotated[
        Path,
        typer.Option(
            "--task",
            help="Task definition YAML file.",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    json_output: Annotated[
        Path | None,
        typer.Option(
            "--json-output",
            help="Optional path for the structured JSON report.",
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Run a baseline/candidate compatibility check."""
    try:
        adapters = {
            baseline: _create_adapter(baseline),
            candidate: _create_adapter(candidate),
        }
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
            disable=not console.is_terminal,
        ) as progress:
            progress_task = progress.add_task(
                "Starting compatibility run",
                total=None,
            )

            def update_progress(message: str) -> None:
                progress.update(
                    progress_task,
                    description=message,
                    refresh=True,
                )

            result = asyncio.run(
                run_compatibility(
                    adapters=adapters,
                    contract_path=contract,
                    task_path=task,
                    source_repository=repo,
                    baseline_name=baseline,
                    candidate_name=candidate,
                    progress_callback=update_progress,
                )
            )
        if json_output is not None:
            _write_json_report(json_output, result)
    except (
        ContractValidationError,
        RunnerConfigurationError,
        TaskValidationError,
    ) as exc:
        typer.secho(f"Run configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, ValueError) as exc:
        typer.secho(f"Run failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.secho(f"Run execution error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    _show_compatibility_report(result)
    if not result.baseline.succeeded or not result.candidate.succeeded:
        raise typer.Exit(code=2)
    if not result.passed:
        raise typer.Exit(code=1)
