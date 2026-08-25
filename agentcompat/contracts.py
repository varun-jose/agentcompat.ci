"""Engineering contract loading and validation."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from agentcompat.models import AgentContract


class ContractValidationError(ValueError):
    """Raised when a contract cannot be loaded or validated."""


def _format_validation_error(exc: ValidationError) -> str:
    """Format Pydantic errors with YAML-friendly dotted field paths."""
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        messages.append(f"{location or 'contract'}: {error['msg']}")
    return "\n".join(messages)


def load_contract(path: Path) -> AgentContract:
    """Load and validate an AgentCompat CI contract from YAML."""
    try:
        raw_contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractValidationError(f"could not read contract: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContractValidationError(f"invalid YAML: {exc}") from exc

    try:
        return AgentContract.model_validate(raw_contract)
    except ValidationError as exc:
        raise ContractValidationError(_format_validation_error(exc)) from exc
