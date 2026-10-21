"""In-memory fault-injection adapter and an explicitly unavailable live adapter."""
from __future__ import annotations

import copy
from .model import FOLDER, SafetyError, WITHDRAWN_ID, digest, snapshot


def fixture_state(file_id, name, mime_type="application/pdf", parents=("origin",)):
    return {"id": file_id, "name": name, "mime_type": mime_type, "parents": sorted(parents),
            "version": "1", "modified_time": "2026-01-01T00:00:00Z", "trashed": False,
            "permissions": [{"id": "fixture-owner", "type": "user", "role": "owner"}],
            "owners": ["fixture-owner"], "drive_id": None,
            "capabilities": {"can_rename": True, "can_move": True, "can_add_children": True},
            "access": {"complete": True, "effective": ["fixture-owner"], "inherited": [], "restrictions": {}},
            "subtree": {"complete": True, "members": []}}


class MockAdapter:
    """Atomic CAS is a fixture capability; the discovered connector does not expose it."""
    is_mock = True

    def __init__(self, states):
        self.states = copy.deepcopy(states)
        self.calls = []
        self.read_calls = []
        self.failure = None
        self._terminal = set()
        self._pending = {}
        self._readback_failure = False

    def get(self, file_id):
        if file_id == WITHDRAWN_ID:
            raise SafetyError("EXCLUDED")
        self.read_calls.append(file_id)
        if self._readback_failure:
            self._readback_failure = False
            raise TimeoutError("fixture readback timeout")
        return copy.deepcopy(self.states[file_id])

    def preflight(self, op, before, excluded):
        parent_ids = set(before["parents"]) | set(op.target.get("parents", []))
        if ({op.file_id} | parent_ids) & set(excluded):
            raise SafetyError("EXCLUDED")
        parents = {key: self.get(key) for key in sorted(parent_ids)}
        after = op.intended(before)
        descendants = {m["id"] for m in before["subtree"].get("members", [])}
        # This mock simulates inherited access changes, without modifying any permissions API.
        if op.action == "MOVE":
            target_access = [parents[key]["access"] for key in after["parents"]]
            if target_access and any(a != before["access"] for a in target_access):
                after["access"] = copy.deepcopy(target_access[0])
        collisions = [key for key, state in self.states.items() if key != op.file_id
                      and not state["trashed"] and state["name"] == after["name"]
                      and set(state["parents"]) & set(after["parents"])]
        return {"complete": True, "rollback_possible": True, "parents": parents,
                "collision_ids": sorted(collisions), "no_cycle": not bool(set(after["parents"]) & (descendants | {op.file_id})),
                "descendants_complete": before["subtree"].get("complete") is True,
                "descendant_ids": sorted(descendants), "expected_after": after}

    def apply(self, op, before, check, attempt_key):
        if attempt_key in self._terminal or attempt_key in self._pending:
            raise SafetyError("ATTEMPT_KEY_ALREADY_USED")
        self.calls.append({"operation_id": op.operation_id, "attempt_key": attempt_key, "target": copy.deepcopy(op.target)})
        if self.failure == "pending":
            self._pending[attempt_key] = (op, copy.deepcopy(before), copy.deepcopy(check))
            self.failure = None
            raise TimeoutError("fixture request may still complete")
        if self.failure == "before_apply":
            self._terminal.add(attempt_key)
            self.failure = None
            raise RuntimeError("fixture terminal rejection")
        if snapshot(self.states[op.file_id]) != before or self.preflight(op, before, {WITHDRAWN_ID}) != check:
            self._terminal.add(attempt_key)
            raise SafetyError("ATOMIC_PRECONDITION_FAILED")
        result = copy.deepcopy(check["expected_after"])
        result["version"] = str(int(before["version"]) + 1)
        result["modified_time"] = "2026-01-01T00:00:%02dZ" % int(result["version"])
        self.states[op.file_id] = result
        self._terminal.add(attempt_key)
        if self.failure == "after_apply":
            self.failure = None
            raise TimeoutError("fixture response lost after success")
        if self.failure == "readback":
            self.failure = None
            self._readback_failure = True
        return {"request_id": attempt_key, "terminal": True, "simulation": True}

    def terminal(self, attempt_key):
        return attempt_key in self._terminal

    def settle_pending(self, attempt_key, apply=False):
        """Explicit fixture event, never inferred from a read of unchanged metadata."""
        op, before, check = self._pending.pop(attempt_key)
        if apply:
            return self.apply(op, before, check, attempt_key)
        self._terminal.add(attempt_key)


class AgentConnectorAdapter:
    """Honest seam: the local Python process has no authenticated connector transport.

    Tool results may be imported as private read-only receipts via bridge.py. No method
    below invokes a tool, REST endpoint, browser, or credentials. Live action remains off.
    """
    is_mock = False

    def get(self, file_id):
        if file_id == WITHDRAWN_ID:
            raise SafetyError("EXCLUDED")
        raise SafetyError("AGENT_RECEIPT_REQUIRED")

    def preflight(self, *args):
        raise SafetyError("INCOMPLETE_EFFECTIVE_ACCESS_AND_CONCURRENCY_GUARD")

    def apply(self, *args):
        raise SafetyError("LIVE_EXECUTION_DISABLED")

    def terminal(self, *args):
        return False


def assess_connector_metadata(raw):
    """Report observed connector omissions; don't fill them with guessed defaults."""
    aliases = {"id": "id", "name": "title", "mime_type": "mime_type", "parents": "parent_ids",
               "version": "version", "modified_time": "modified_time", "trashed": "trashed",
               "owners": "owners", "drive_id": "drive_id", "capabilities": "capabilities",
               "permissions": "permissions"}
    missing = [key for key, raw_key in aliases.items() if raw_key not in raw]
    return {"read_only": True, "execution_ready": False, "missing_fields": missing,
            "unproven": ["complete_effective_access", "permission_inheritance_provenance",
                         "complete_descendant_membership", "ancestor_access_and_drive_restrictions",
                         "rollback_capability", "conditional_metadata_update", "request_terminal_evidence"],
            "normalized_known_fields": {key: raw[raw_key] for key, raw_key in aliases.items() if raw_key in raw}}
