"""Public client contract for optional Relay memory retrieval.

The client receives only coarse setup phases and progress.  This module must
not expose private construction, hierarchy, ranking, or retrieval mechanics.
"""

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from .client_errors import ValidationError


_MODE_FIELDS = frozenset({"memory_retrieval_enabled"})
_PROGRESS_FIELDS = frozenset({"stage", "completed_units", "total_units"})
_PHASES = {
    "scanning_sources": (0, 10, "Scanning selected sources"),
    "transferring_selected_content": (10, 50, "Importing selected content"),
    "organizing_private_memory": (50, 99, "Organizing private memory"),
    "ready": (100, 100, "Memory retrieval is ready"),
}


@dataclass(frozen=True)
class MemoryModeSelection:
    enabled: bool

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "MemoryModeSelection":
        if not isinstance(payload, Mapping):
            raise ValidationError("memory mode selection must be an object")
        if set(payload) != _MODE_FIELDS:
            raise ValidationError(
                "memory mode selection requires only memory_retrieval_enabled"
            )
        enabled = payload.get("memory_retrieval_enabled")
        if not isinstance(enabled, bool):
            raise ValidationError("memory_retrieval_enabled must be boolean")
        return cls(enabled)

    def public_payload(self) -> Dict[str, object]:
        return {"memory_retrieval_enabled": self.enabled}


@dataclass(frozen=True)
class MemoryPreparationProgress:
    stage: str
    completed_units: int = 0
    total_units: Optional[int] = None

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "MemoryPreparationProgress":
        if not isinstance(payload, Mapping):
            raise ValidationError("memory preparation progress must be an object")
        unsupported = sorted(set(payload) - _PROGRESS_FIELDS)
        if unsupported or "stage" not in payload:
            raise ValidationError("memory preparation progress fields are invalid")
        stage = payload.get("stage")
        if not isinstance(stage, str) or stage not in _PHASES:
            raise ValidationError("memory preparation stage is invalid")
        completed = payload.get("completed_units", 0)
        total = payload.get("total_units")
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise ValidationError("completed_units must be a nonnegative integer")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total <= 0
        ):
            raise ValidationError("total_units must be a positive integer when known")
        if total is not None and completed > total:
            raise ValidationError("completed_units cannot exceed total_units")
        if stage == "ready" and total is not None and completed != total:
            raise ValidationError("ready progress must be complete")
        return cls(stage, completed, total)

    @property
    def indeterminate(self) -> bool:
        return self.stage != "ready" and self.total_units is None

    @property
    def percentage(self) -> Optional[int]:
        start, end, unused_label = _PHASES[self.stage]
        del unused_label
        if self.stage == "ready":
            return 100
        if self.total_units is None:
            return None
        span = end - start
        return min(end, start + int(span * self.completed_units / self.total_units))

    def public_payload(self) -> Dict[str, object]:
        unused_start, unused_end, label = _PHASES[self.stage]
        del unused_start, unused_end
        return {
            "stage": self.stage,
            "label": label,
            "percentage": self.percentage,
            "indeterminate": self.indeterminate,
            "ready": self.stage == "ready",
        }


def memory_mode_setup_contract() -> Dict[str, object]:
    """Return the customer-visible toggle, claims, and preparation policy."""

    return {
        "field": "memory_retrieval_enabled",
        "default_enabled": False,
        "customer_can_change": True,
        "disabled": {
            "title": "Execution optimization",
            "claim": "Save up to about 50% of premium-model compute.",
            "maximum_savings_percent": 50,
            "metric": "premium_model_compute",
            "requires_memory_preparation": False,
            "relay_rate_source": "server_account",
        },
        "enabled": {
            "title": "Memory retrieval + execution optimization",
            "claim": (
                "Save up to about 90% of total model compute on long, continuous, "
                "history-rich tasks."
            ),
            "maximum_savings_percent": 90,
            "metric": "total_model_compute",
            "requires_memory_preparation": True,
            "relay_rate_source": "server_account",
            "memory_value": {
                "headline": "Near-unlimited long-term memory",
                "description": (
                    "Your history can keep growing while Relay retrieves only "
                    "what each task needs."
                ),
                "subject_to_account_storage_limits": True,
            },
        },
        "preparation": {
            "separate_setup_charge": False,
            "subject_to_account_import_limits": True,
            "progress_stages": tuple(_PHASES),
            "size_notice": "More memory takes longer to prepare.",
        },
    }


def memory_preparation_stages() -> Tuple[str, ...]:
    return tuple(_PHASES)
