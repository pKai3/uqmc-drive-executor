"""Readable destination CLI output plus a private, complete JSONL transcript.

This is presentation only. The execution journal remains authoritative; console
or transcript errors must never cause a file operation to be retried.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


def concise(value, limit=100):
    text = " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def counts_text(counts):
    return ", ".join(f"{value} {key.lower().replace('_', ' ')}"
                     for key, value in sorted((counts or {}).items()) if value) or "no changes"


class ConsoleReporter:
    def __init__(self, *, json_output=False, stream=None, clock=time.monotonic):
        self.json_output = json_output
        self.stream = stream if stream is not None else sys.stdout
        self.clock, self.started = clock, clock()
        self.log, self.log_path = None, None
        self.lock = threading.RLock()
        self.execution_started = False
        self.destination_reads = 0
        self.counts = {}
        self.log_error = False

    def open_log(self, run_directory):
        directory = Path(run_directory) / "console-logs"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.log_path = directory / (stamp + "-" + uuid.uuid4().hex[:12] + ".jsonl")
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.log = os.fdopen(fd, "w", encoding="utf-8")
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def elapsed(self):
        seconds = max(0, int(self.clock() - self.started))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def line(self, value):
        print(f"[{self.elapsed()}] {value}", file=self.stream, flush=True)

    def write_log(self, kind, value):
        if self.log is None or self.log_error:
            return
        try:
            self.log.write(json.dumps({"type": kind,
                "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "elapsed_seconds": round(self.clock() - self.started, 3), "data": value},
                ensure_ascii=False) + "\n")
            self.log.flush()
            os.fsync(self.log.fileno())
        except OSError:
            self.log_error = True
            # Durable execution evidence is independent of this auxiliary log.
            if self.json_output:
                print(json.dumps({"warning": "CONSOLE_TRANSCRIPT_WRITE_FAILED"}), file=self.stream, flush=True)
            else:
                self.line("Warning: console transcript write failed; execution journal is separate.")

    def progress(self, value):
        with self.lock:
            self.write_log("progress", value)
            if self.json_output:
                print(json.dumps(value, ensure_ascii=False), file=self.stream, flush=True)
                return
            stage = value.get("stage", value.get("event"))
            if stage == "stage_started":
                self.line(concise(value.get('stage_name', 'Stage')) + '…')
            elif stage == "stage_finished":
                status = 'complete' if value.get('outcome') == 'COMPLETE' else 'failed'
                self.line(f"{concise(value.get('stage_name', 'Stage'))}: {status} in {value.get('duration_seconds', 0):.3f}s.")
            elif stage == "account_verified":
                self.line("Google account verified: " + concise(value.get("account", "")))
            elif stage == "fresh_sheet":
                self.line(f"Fresh plan: {concise(value.get('section', ''))}; {value.get('selected_identities', '?')} identities.")
            elif stage == "source_metadata":
                n, total = value.get("checked", 0), value.get("total", "?")
                if n == 1 or n == total or n % 10 == 0:
                    self.line(f"Checking source metadata: {n}/{total}.")
            elif stage == "destination_metadata":
                self.destination_reads += 1
                if self.destination_reads == 1 or self.destination_reads % 10 == 0:
                    self.line(f"Checking destination folders: {self.destination_reads}.")
            elif stage == "verified_history_skipped":
                self.line(f"Preserving {value.get('identities', 0)} completed identities; {value.get('remaining_live_identities', 0)} checked live.")
            elif stage == "starting_name_warning":
                self.line("Warning: starting name differs for " + concise(value.get("source_id", "item"))
                          + ": " + concise(value.get("observed_name", "")) + " → "
                          + concise(value.get("approved_target_name", ""))
                          + "; rollback will preserve the observed name.")
            elif stage == "folder_verified":
                self.line("Folder verified: " + concise(value.get("path", "")))
            elif stage == "item_started":
                if not self.execution_started:
                    self.execution_started = True
                    self.line(f"{value.get('mode', 'EXECUTION').title()}: processing {value.get('total_items', '?')} items.")
            elif stage == "item_finished":
                state = value.get("state", "UNKNOWN")
                self.counts[state] = self.counts.get(state, 0) + 1
                name = (value.get("observed") or {}).get("name") or value.get("source_id", "item")
                count = f"{value.get('completed_items', '?')}/{value.get('total_items', '?')}"
                reason = value.get("reason", value.get("reason_code"))
                suffix = "; " + concise(reason, 100) if reason else ""
                self.line(f"{count} {state.lower().replace('_', ' ')}: {concise(name)}{suffix}")
            elif stage == "request_failed":
                error = value.get("request_error") or {}
                self.line(f"Request failed for {concise(value.get('source_id', 'item'))}: {concise(error.get('code', 'UNKNOWN'))}. Checking current state before retry.")
            elif stage == "read_retry":
                self.line(f"Read retry {value.get('attempt', '?')}: {concise(value.get('method', 'read'))}; {concise(value.get('reason', ''))}.")
            elif stage == "scheduling_stopped":
                self.line("Scheduling stopped: " + concise(value.get("reason", "unknown reason")))
            elif stage == "execution_finished":
                self.line(f"Execution saved: {counts_text(value.get('counts'))}; checkpoint {value.get('journal_sequence', '?')}.")
                self.line("Publishing verified status and viewer (local HTML → configured server → Drive)…")
            elif stage:
                self.line(concise(str(stage).replace("_", " ").capitalize()) + ".")

    def finish(self, result):
        with self.lock:
            self.write_log("result", result)
            if self.json_output:
                print(json.dumps(result, ensure_ascii=False), file=self.stream, flush=True)
                return
            label = "Preview" if result.get("preview_only") else "Result"
            self.line(f"{label}: {counts_text(result.get('counts'))}.")
            if result.get("preview_only"):
                self.line(f"Remaining effects: {result.get('required_rename_effects', 0)} renames, {result.get('required_move_effects', 0)} moves; {len(result.get('missing_folders', []))} folders to create.")
            blockers = result.get("blockers") or []
            for blocker in blockers[:8]:
                name = (blocker.get("observed") or {}).get("name") or blocker.get("source_id", "item")
                self.line(f"Blocked: {concise(name)}; {concise(blocker.get('reason', blocker.get('reason_code', 'See JSON log')))}")
            if len(blockers) > 8:
                self.line(f"{len(blockers) - 8} more blockers are recorded in the JSON log.")
            if result.get("revision_stop_reason"):
                self.line("Scheduling stopped: " + concise(result["revision_stop_reason"]))
            publication = result.get("publication")
            if publication is None and ("state" in result or
                    str(result.get("result", "")).startswith("PUBLICATION_")):
                publication = result  # Publication-only delivery result.
            if publication is not None:
                if isinstance(publication, dict):
                    state = publication.get("result", publication.get("state", publication.get("status", "UNKNOWN")))
                    error = publication.get("error")
                else:
                    state, error = publication, None
                self.line("Publication: " + concise(state) + ("; " + concise(error, 180) if error else "") + ".")
                if error == "SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED":
                    self.line("Publication stopped: section membership conflicts with the public display allowlist. Saved migration results are unchanged.")
                if isinstance(publication, dict):
                    for key, label in (("sheet", "Spreadsheet"), ("server_sync", "Server sync"), ("html", "Drive HTML")):
                        stage = publication.get(key)
                        if not isinstance(stage, dict):
                            continue
                        reason = stage.get("reason", stage.get("error", stage.get("error_class")))
                        stage_result = stage.get("result")
                        if not reason and isinstance(stage_result, dict):
                            reason = stage_result.get("reason", stage_result.get("result"))
                        self.line(label + ": " + concise(stage.get("state", "UNKNOWN")) +
                                  ("; " + concise(reason, 180) if reason and stage.get("state") != "VERIFIED" else "") + ".")
            if result.get("journal_sequence") is not None:
                self.line(f"Checkpoint: {result['journal_sequence']}.")
            if result.get("run_id"):
                self.line("Run: " + concise(result["run_id"]))
            if result.get("backup"):
                self.line("Backup: " + str(result["backup"]))
            self.line("Elapsed: " + self.elapsed() + ".")
            if self.log_path:
                self.line("Full JSON log: " + str(self.log_path))

    def error(self, value):
        with self.lock:
            self.write_log("error", value)
            if self.json_output:
                print(json.dumps(value, ensure_ascii=False), file=self.stream, flush=True)
            else:
                self.line("Error: " + concise(value.get("error", "Unknown error"), 220))
                if self.log_path:
                    self.line("Full JSON log: " + str(self.log_path))

    def close(self):
        with self.lock:
            if self.log is not None:
                self.log.close()
                self.log = None
