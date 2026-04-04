"""Job state machine with validated transitions."""

from __future__ import annotations

import enum


class JobState(str, enum.Enum):
    """All possible states in a job's lifecycle."""

    CREATED = "created"
    UPLOADING_INPUTS = "uploading_inputs"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"


# Valid state transitions. A job can only move from key -> one of the values.
VALID_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.CREATED: {JobState.UPLOADING_INPUTS, JobState.FAILED},
    JobState.UPLOADING_INPUTS: {JobState.RUNNING, JobState.FAILED},
    JobState.RUNNING: {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
        JobState.CHECKPOINTING,
    },
    JobState.CHECKPOINTING: {JobState.RUNNING, JobState.FAILED, JobState.CANCEL_REQUESTED},
    JobState.CANCEL_REQUESTED: {JobState.CANCELED, JobState.FAILED},
    JobState.COMPLETED: set(),
    JobState.FAILED: set(),
    JobState.CANCELED: set(),
}

TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELED})


def can_transition(from_state: JobState, to_state: JobState) -> bool:
    """Check whether a state transition is valid."""
    return to_state in VALID_TRANSITIONS.get(from_state, set())


def is_terminal(state: JobState) -> bool:
    """Return True if the state is a final state (no further transitions)."""
    return state in TERMINAL_STATES


def validate_transition(from_state: JobState, to_state: JobState) -> None:
    """Raise ValueError if the transition is invalid."""
    if not can_transition(from_state, to_state):
        allowed = VALID_TRANSITIONS.get(from_state, set())
        raise ValueError(
            f"Invalid state transition: {from_state.value} -> {to_state.value}. "
            f"Allowed transitions from {from_state.value}: "
            f"{', '.join(s.value for s in sorted(allowed, key=lambda s: s.value)) or 'none (terminal state)'}"
        )
