"""Language-neutral acceptance cards and dual-track status derivation."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


CARD_KINDS = {"test", "check", "ui", "manual"}
CARD_STATUSES = {"PASS", "FAIL", "NOT_CHECKED", "BLOCKED"}
TRACK_STATUSES = {
    "implementation_pending",
    "implementation_done",
    "acceptance_pending",
    "acceptance_verified",
}


class TrackState(Enum):
    IMPLEMENTATION_PENDING = "implementation_pending"
    IMPLEMENTATION_DONE = "implementation_done"
    ACCEPTANCE_PENDING = "acceptance_pending"
    ACCEPTANCE_VERIFIED = "acceptance_verified"

    def __new__(cls, value):
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    @classmethod
    def _missing_(cls, value):
        raise ValueError(f"unknown track status: {value}")


@dataclass(frozen=True)
class AcceptanceCard:
    id: str
    kind: str
    required: bool
    status: str
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    observed: str = ""
    evidence: list[str] = field(default_factory=list)
    not_checked_reason: str | None = None
    next_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "required": self.required,
            "status": self.status,
            "steps": list(self.steps),
            "expected": self.expected,
            "observed": self.observed,
            "evidence": list(self.evidence),
        }
        if self.not_checked_reason is not None:
            result["not_checked_reason"] = self.not_checked_reason
        if self.next_action is not None:
            result["next_action"] = self.next_action
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcceptanceCard":
        validate_acceptance_card(value)
        return cls(
            id=value["id"], kind=value["kind"], required=value["required"],
            status=value["status"], steps=list(value.get("steps", [])),
            expected=value.get("expected", ""), observed=value.get("observed", ""),
            evidence=list(value.get("evidence", [])),
            not_checked_reason=value.get("not_checked_reason"),
            next_action=value.get("next_action"),
        )


def _text(value: Any, name: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")


def validate_acceptance_card(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("acceptance card must be an object")
    for name in ("id", "kind", "status"):
        if name not in value:
            raise ValueError(f"missing card field: {name}")
    _text(value["id"], "id", allow_empty=False)
    if value["kind"] not in CARD_KINDS:
        raise ValueError(f"unknown card kind: {value['kind']}")
    if value["status"] not in CARD_STATUSES:
        raise ValueError(f"unknown card status: {value['status']}")
    if not isinstance(value.get("required"), bool):
        raise ValueError("required must be boolean")
    for name in ("steps", "evidence"):
        items = value.get(name, [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"{name} must be a list of strings")
    for name in ("expected", "observed"):
        if name in value:
            _text(value[name], name)
    if value["status"] == "NOT_CHECKED":
        _text(value.get("not_checked_reason"), "not_checked_reason", allow_empty=False)
        _text(value.get("next_action"), "next_action", allow_empty=False)
    if value["status"] == "PASS" and value.get("required") and not value.get("evidence"):
        raise ValueError("required PASS cards need evidence")
    for name in ("not_checked_reason", "next_action"):
        if name in value and value[name] is not None:
            _text(value[name], name, allow_empty=False)


def derive_project_status(tasks: list[dict[str, Any]], *, final_regression: bool, review: bool) -> str:
    """Derive the project gate without changing task or evidence state."""
    if not tasks:
        return "execute"
    implementations = [task.get("implementation", {}).get("status") for task in tasks]
    acceptances = [task.get("acceptance_track", task.get("acceptance", {})).get("status") for task in tasks]
    if any(status != "implementation_done" for status in implementations):
        return "execute"
    if any(status != "acceptance_verified" for status in acceptances):
        return "implementation_ready"
    if not final_regression or not review:
        return "acceptance_ready"
    return "ready"


def acceptance_track_status(cards: list[dict[str, Any]]) -> str:
    """Return the acceptance track state for required cards."""
    for card in cards:
        validate_acceptance_card(card)
    required = [card for card in cards if card["required"]]
    if not required:
        return "acceptance_pending"
    if all(card["status"] == "PASS" for card in required):
        return "acceptance_verified"
    return "acceptance_pending"
