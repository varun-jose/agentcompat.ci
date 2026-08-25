"""Command-line interface for AgentCompat CI."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.align import Align
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from agentcompat import __version__
from agentcompat.adapters import (
    AgentAdapter,
    CodexAdapter,
    GeminiAdapter,
    KiroAdapter,
)
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
        str(contract.rules.changed_files.max)
        if contract.rules.changed_files.max is not None
        else "Not set",
    )
    table.add_row(
        "Count Git-ignored files",
        "Yes" if contract.rules.changed_files.include_ignored else "No",
    )
    table.add_row(
        "Changed-file exclusions",
        _format_items(contract.rules.changed_files.exclude),
    )
    table.add_row(
        "Forbidden dependencies",
        _format_items(contract.rules.forbidden_dependencies),
    )
    table.add_row(
        "Dependency manifests",
        _format_items(
            [
                f"{manifest.path} ({manifest.role})"
                for manifest in contract.rules.dependency_manifests
            ]
        ),
    )
    drift = contract.rules.dependency_drift
    table.add_row(
        "Dependency removals forbidden",
        "Yes" if drift.removals_forbidden else "No",
    )
    table.add_row(
        "Dependency versions fixed",
        "Yes" if drift.versions_must_not_change else "No",
    )
    table.add_row(
        "Dependency sources fixed",
        "Yes" if drift.sources_must_not_change else "No",
    )
    table.add_row(
        "Lockfiles fixed",
        "Yes" if drift.lockfiles_must_not_change else "No",
    )
    table.add_row(
        "Lockfiles co-updated",
        "Yes" if drift.lockfiles_must_be_updated_for_dependency_changes else "No",
    )
    table.add_row("Lockfiles", _format_items(drift.lockfiles))

    console.print("[bold green]Contract is valid[/bold green]")
    console.print(table)


def _create_adapter(agent_name: str) -> AgentAdapter:
    """Create one of the agent adapters supported by the MVP CLI."""
    if agent_name == "codex":
        return CodexAdapter()
    if agent_name == "gemini":
        return GeminiAdapter()
    if agent_name == "kiro":
        return KiroAdapter()
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
    """Format ordinary Git changes and visible Git-ignored artifacts."""
    changes = [
        *(f"modified: {path}" for path in run.diff.changed_files),
        *(f"added: {path}" for path in run.diff.added_files),
        *(f"deleted: {path}" for path in run.diff.deleted_files),
        *(f"ignored: {path}" for path in run.diff.ignored_files),
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


def _styled_status(status: str) -> str:
    """Add a consistent icon and colour to a terminal status value."""
    escaped_status = escape(status)
    if status.startswith("PASS"):
        return f"[bold green]✓ {escaped_status}[/bold green]"
    if status.startswith(("FAIL", "ERROR")):
        return f"[bold red]✗ {escaped_status}[/bold red]"
    if status == "NOT REQUIRED":
        return f"[dim]— {escaped_status}[/dim]"
    return f"[bold yellow]? {escaped_status}[/bold yellow]"


def _evaluation_details(finding: EvaluationResult | None) -> str:
    """Return a concise human-readable explanation for one check."""
    if finding is None:
        return "No evaluator result was produced."
    return finding.summary


def _show_compatibility_report(result: CompatibilityResult) -> None:
    """Render a spacious, sectioned Rich terminal compatibility report."""
    forbidden = _find_evaluation(result, "forbidden_paths")
    required = _find_evaluation(result, "required_paths")
    build = _find_evaluation(result, "build_must_pass")
    tests = _find_evaluation(result, "tests_must_pass")
    max_changed = _find_evaluation(result, "max_changed_files")
    forbidden_dependencies = _find_evaluation(result, "forbidden_dependencies")
    dependency_removals = _find_evaluation(result, "dependency_removals")
    dependency_versions = _find_evaluation(result, "dependency_version_changes")
    dependency_sources = _find_evaluation(result, "dependency_source_changes")
    lockfile_changes = _find_evaluation(result, "dependency_lockfile_changes")
    lockfile_updates = _find_evaluation(result, "dependency_lockfile_updates")
    critical = _critical_failures(result)
    final_status = "PASS" if result.passed else "FAIL"
    compatibility = _compatibility_percentage(result)
    border_style = "green" if result.passed else "red"

    heading = (
        f"[bold white]{escape(result.contract.project.name)}[/bold white]"
        f"  [dim]•[/dim]  [cyan]{escape(_task_label(result))}[/cyan]"
    )
    console.print()
    console.print(
        Panel(
            Align.center(heading),
            title="[bold cyan]AgentCompat CI compatibility report[/bold cyan]",
            subtitle=f"commit {result.original_commit_sha[:8]}",
            box=box.DOUBLE,
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    runs = Table(
        title="[bold]Agent runs[/bold]",
        title_justify="left",
        box=box.ROUNDED,
        expand=True,
        show_lines=True,
        padding=(0, 1),
        header_style="bold cyan",
    )
    runs.add_column("Role", no_wrap=True, width=19)
    runs.add_column("Result", ratio=1)
    runs.add_row(
        "Baseline execution",
        f"[bold]{escape(result.baseline.agent_name)}[/bold]  "
        f"{_styled_status(_execution_status(result.baseline))}\n\n"
        "[dim]Baseline changed files[/dim]\n"
        f"{escape(_changed_files(result.baseline))}",
    )
    runs.add_row(
        "Candidate execution",
        f"[bold]{escape(result.candidate.agent_name)}[/bold]  "
        f"{_styled_status(_execution_status(result.candidate))}\n\n"
        "[dim]Candidate changed files[/dim]\n"
        f"{escape(_changed_files(result.candidate))}",
    )
    console.print(runs)
    console.print()

    checks = Table(
        title="[bold]Deterministic compatibility checks[/bold]",
        title_justify="left",
        box=box.ROUNDED,
        expand=True,
        padding=(0, 1),
        header_style="bold cyan",
    )
    checks.add_column("Check", no_wrap=True, width=25)
    checks.add_column("Status", no_wrap=True, width=12)
    checks.add_column("Details", ratio=1)
    checks.add_row(
        "Build status",
        _styled_status(_evaluation_status(build)),
        escape(_evaluation_details(build)),
    )
    checks.add_row(
        "Test status",
        _styled_status(_evaluation_status(tests)),
        escape(_evaluation_details(tests)),
    )
    checks.add_row(
        "Forbidden path violations",
        _styled_status(_evaluation_status(forbidden)),
        (
            escape(_format_items(forbidden.evidence))
            if forbidden is not None and forbidden.evidence
            else escape(_evaluation_details(forbidden))
        ),
    )
    checks.add_row(
        "Required path compliance",
        _styled_status(_evaluation_status(required)),
        escape(
            _evaluation_details(required)
            if required is not None and required.passed
            else _required_path_status(required)
        ),
    )
    checks.add_row(
        "Changed-file limit",
        _styled_status(_evaluation_status(max_changed)),
        escape(_evaluation_details(max_changed)),
    )
    checks.add_row(
        "Forbidden dependencies",
        _styled_status(_evaluation_status(forbidden_dependencies)),
        escape(_evaluation_details(forbidden_dependencies)),
    )
    for label, finding in (
        ("Dependency removals", dependency_removals),
        ("Dependency versions", dependency_versions),
        ("Dependency sources", dependency_sources),
        ("Lockfile changes", lockfile_changes),
        ("Required lock updates", lockfile_updates),
    ):
        checks.add_row(
            label,
            _styled_status(_evaluation_status(finding)),
            escape(
                (f"{_evaluation_details(finding)} {_format_items(finding.evidence)}")
                if finding is not None and finding.evidence
                else _evaluation_details(finding)
            ),
        )
    console.print(checks)
    console.print()

    critical_details = (
        "\n".join(f"• {finding.name}: {finding.summary}" for finding in critical)
        if critical
        else "None"
    )
    final_markup = (
        f"[bold green]✓ Final result: {final_status}[/bold green]"
        if result.passed
        else f"[bold red]✗ Final result: {final_status}[/bold red]"
    )
    verdict = Table.grid(expand=True, padding=(0, 1))
    verdict.add_column(no_wrap=True, ratio=2)
    verdict.add_column(ratio=4)
    verdict.add_row(
        final_markup,
        "",
    )
    verdict.add_row(
        "[bold]Compatibility percentage[/bold]",
        Align.right(f"[bold]{compatibility:.1f}% compatible[/bold]"),
    )
    verdict.add_row(
        "[bold]Critical failures[/bold]",
        escape(critical_details),
    )
    console.print(
        Panel(
            verdict,
            title="[bold]Final verdict[/bold]",
            box=box.HEAVY,
            border_style=border_style,
            padding=(1, 2),
        )
    )
    console.print()


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
