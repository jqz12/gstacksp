"""Explicit, non-destructive migration into the acceptance protocol."""
import copy
import hashlib
import json


def migrate_state(state: dict) -> dict:
    """Return a migrated copy; never infer acceptance success from old task status."""
    result = copy.deepcopy(state)
    before = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
    result["acceptance_protocol"] = "dual-track"
    result["migration"] = {
        "from_schema": state.get("schema", 1),
        "source_hash": hashlib.sha256(before).hexdigest(),
        "acceptance_inferred": False,
    }
    for task in result.get("tasks", []):
        if "implementation" not in task:
            task["implementation"] = {"status": "implementation_done" if task.get("status") == "done" else "implementation_pending"}
        task.setdefault("acceptance_cards", [])
        task.setdefault("acceptance_track", {"status": "acceptance_pending"})
        task["acceptance_track"]["status"] = "acceptance_pending"
    result["status"] = "needs-verification"
    result.setdefault("migration_history", []).append({
        "event": "acceptance-protocol-migration",
        "source_hash": result["migration"]["source_hash"],
        "acceptance_inferred": False,
    })
    return result
