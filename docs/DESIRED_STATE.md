# Desired-state Drive runner

`uqmc_recovery.desired` runs the approved plan through the Google Drive API in a normal Python loop. It does not need an assistant tool call for each file. Focused fake Drive tests cover recovery and rollback. An earlier bounded parallel trial verified 30 moves; the new caching revision is tested offline only. Installing or authorizing it does not start execution.

## What one item does

1. Read the exact source ID, name, parent IDs and available safety metadata.
2. Compare the observation with the approved original, intermediate and target states. Stop the item on unapproved drift.
3. Skip effects already satisfied. If both name and parent need changing, use one `files.update` with `body.name`, `addParents` and `removeParents`. Otherwise send only the remaining change.
4. Persist the observed preimage and intended result before dispatch, then read back and record the resulting state.

The scheduler retains real folder dependencies: a receiving folder must reach its required state before an incoming item uses it. It does not require every unrelated rename to finish before moves can start. One local writer holds the journal lock. Other Drive clients are not locked by this process.

Mutation commands use a bounded pool (`--workers 10` by default, configurable from 1 to 32). Each worker thread creates its own Google HTTP transport and credentials object from the same existing private token, checks the account, and keeps the 15-second socket timeout. No HTTP client is shared across threads. A single controller retains the process lock while journal/evidence writes serialize; network calls can overlap. A source ID can only be scheduled once per invocation. Potential same-name destination writes serialize so their collision checks do not race with each other.

Receiving folders must be verified at their approved target before dependent items start. A receiving folder is a read-only dependency for those child operations: its exact ID, name, parent location, MIME type, trash state, protected metadata and known `canAddChildren` capability remain checked, but its global version/modified-time/ETag need not remain frozen as siblings arrive. Every child still receives fresh pre-write metadata checks. A numeric version-counter increase alone can proceed when all known operation-relevant fields match; the journal retains both observations and the comparison. Name, parent, MIME, content hashes, size, owners, capabilities, modification time and available ETag changes remain guarded. This exception never authorizes a retry of an unresolved request or reopens a completed effect. Parent reads do not provide an atomic lock against external edits after the check. Rollback reverses these edges. `--source-ids-file private/trial-ids.json` accepts a unique array of approved IDs: an outside dependency is read and must already be satisfied; it is never automatically added to the mutation scope. `--limit` reserves slots for in-flight work so concurrent completions cannot exceed the cap of new changes. Already-target observations do not consume that cap. Cancellation stops new scheduling and pre-dispatch work while allowing dispatched requests to read back and journal their outcomes. A process crash leaves durable attempts for resume reconciliation; neither a cancellation nor a timeout is assumed to mean failure.

Use the existing plan and journal. The current saved checkpoint is 550 with 105 verified renames and no verified moves; its original checkpoint 508 history remains intact. A file already bearing its approved name receives only its remaining move; the historical operation order is not a reason to rename it again.

## Resume and conflicts

Current state and request history are separate. A fresh read showing the approved original name is a known current observation, even if an older request returned an ambiguous error. A read showing the target avoids another update; it does not retrospectively prove which request caused that state.

The Google HTTP transport uses a 15-second socket timeout for account, metadata, permission and token-refresh requests. This bounds a blocked connection/read; it is not a strict 15-second wall-clock deadline for an entire response or batch. A response that continues transferring data, multiple network phases, or multiple paginated requests can take longer overall. Interactive OAuth consent keeps its separate five-minute allowance.

Read requests have no hidden client retries. The runner explicitly retries transient reads within the configured attempt budget; exhausted post-write reads leave an `UNCERTAIN` result. The CLI immediately flushes item start/result lines and safe endpoint/item error context; write failures are reported before reconciliation. Preview honors `--limit` (for example `--limit 1`) and checks Ctrl-C/termination requests between items, retaining completed results. An in-flight read must return or time out before that orderly stop can finish.

Transient write failures have a bounded attempt budget (default three). Backoff grows exponentially with jitter, capped at 30 seconds. HTTP 429/eligible 5xx and structured 403 `rateLimitExceeded`/`userRateLimitExceeded` can retry; permission-denied 403 errors cannot. Before another attempt, read again and recalculate the remaining change. Never blindly replay the previous request. A write timeout has an unknown request outcome; current-state readback decides whether the target is already satisfied, a bounded retry is justified, or the item must stop. If a read is unavailable, an item has unapproved changes, or the remaining outcome is unsafe, stop that item and its dependents with a reason. Independent work need not stop globally.

For a blocked item, inspect the reported observation and reason, resolve the external conflict, then run preview and resume. If the desired change itself has changed, obtain a revised approved plan; do not edit the journal to erase the conflict. The program does not promise exactly-once execution or prove that every timed-out remote request was never processed.

Future structured Google errors retain a bounded `provider_error` with reason codes and a redacted message in private progress logs and journal receipts. Raw request URLs, headers, response bodies and exception representations are never saved. Known in-memory credentials, URL and common token forms are redacted; malformed or oversized error bodies are omitted. This does not reconstruct previously discarded diagnostics or change retry authorization.

Where an HTTP ETag is available, the update can carry `If-Match`. Provider behavior still needs a live test. Fresh reads without an enforced conditional write leave a race between the final read and update; they are not an atomic concurrency guarantee.

## Commands

Install the existing optional dependencies with `python3 -m pip install -e '.[direct]'`. Supply the existing private plan and journal paths rather than creating replacement evidence.

```sh
python3 -m uqmc_recovery.desired preview \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json

python3 -m uqmc_recovery.desired apply --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json \
  --workers 10 --limit 10 --max-attempts 3 --export-dir private/exports

python3 -m uqmc_recovery.desired resume --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json \
  --workers 10 --limit 10 --max-attempts 3 --export-dir private/exports
```

`--limit` bounds the invocation; it does not expand the approved source set. Export files are local status projections, not spreadsheet writes. Publish the intended status rows in batches, deduplicate log events by Event ID, and preserve human approval cells. A publication failure must not trigger another Drive update. Never publish raw evidence or credentials through the HTML viewer.

The local status format is `uqmc.desired.status.v1`, with explicit coverage limited to items observed during that invocation. It is not a replacement for the existing strict `uqmc.execution.snapshot.v1` workbook/HTML snapshot. The second export uses the existing 26 Operation Log columns; a later sync must map these local results, retain historical evidence and merge by source ID before publishing. No Sheets scope or automatic sync is included.

Preview prints the authenticated account returned by Drive, plus actual source and parent capabilities. No club account is hard-coded. Add `--expect-account YOUR_GOOGLE_EMAIL` to preview/apply/resume/rollback to enforce the account you intend. Explicit capability refusals stop the affected item; omitted capabilities remain unverified. Mutation targets are restricted to the approved manifest source IDs; readable destination or ancestor IDs are not automatically writable targets. Broader OAuth metadata access does not expand this allowlist. See Google's [account read endpoint](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get) and [file capability fields](https://developers.google.com/workspace/drive/api/reference/rest/v3/files).

Preview caches successful common-parent metadata/permissions and complete folder listings only within that invocation. Each source still gets fresh metadata and permissions. These shared observations are advisory snapshots from their first read, not a live authorization or an atomic snapshot of Drive. A second preview starts empty. Apply, resume and rollback never use this cache: source preconditions, parent access and destination listings are read again for every transition and retry. No mutation-time cache invalidation is needed because write modes retain no such cache. Folder listings request only IDs, names, MIME types and parent IDs; full source/parent metadata and paginated permissions remain available to drift/access checks.

For the approved First Aid/CPR section, pass `--skip-permission-audit` to the desired-state CLI (or `audit_permissions=False` to both `DesiredRunner` and `GoogleDriveTransport` when using the lower-level name-only workflow). This explicit option skips `permissions.list`, permission-baseline comparisons and inherited-access re-audits; it also omits permission/access-audit fields from metadata responses. It requires the existing frozen section authority to record `SCOPED_EXPOSURE_ACCEPTED`. Other runs retain permission auditing by default. IDs, exact names/parents, content/owner metadata, concurrency checks, required capability fields, API errors, write-ahead journal and reconciliation are unchanged. The mode is recorded in journal events and status output; it never grants permission-edit access or resumes migration by itself.

## Explicit rollback

Rollback is never automatic. It applies only to transitions recorded by this runner, in reverse dependency order. It restores the exact recorded before name and parents only when fresh current metadata matches the recorded applied state. A conflict stops that item rather than overwriting a later manual change. Restarting rollback skips verified restorations.

```sh
python3 -m uqmc_recovery.desired rollback --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json \
  --workers 10 --limit 10 --max-attempts 3 --export-dir private/exports
```

This command does not automatically undo historical renames from the earlier executor. If an already-renamed item receives only a move, its recorded preimage already contains the approved name; reversing that new transition restores its parent while preserving that name. Target observations requiring no update are not new rollback work. Permission propagation is not restored by a metadata rollback.

## Google authorization remains separate

The runner never creates credentials or initiates OAuth automatically. The assistant's connector session is not a reusable program credential. A separately authorized local token is required; the current checkout already has a user-created token. For a new installation:

1. Choose the Google account and Cloud project, enable Drive API, and configure a Desktop app OAuth client and consent settings.
2. Run the explicit login command below and select your intended Google account in the browser. The project owner and the account granting Drive access can differ; your selected account needs access to the sources and destinations. If the OAuth app is in testing, include that selected account as a test user.
3. Place the downloaded OAuth Desktop client JSON in the ignored local `secrets/API_KEY.json` file. Despite its user-chosen filename, its contents must be OAuth Desktop client configuration, not an API key. Keep it and the resulting token out of Git commits, exports and chat. Pass the token path explicitly. Consent does not authorize automatic execution.

```sh
python3 -m uqmc_recovery.google_login \
  --client-json secrets/API_KEY.json \
  --token secrets/token.json
```

This command is the only entry point that initiates OAuth. It uses Google's installed-app browser flow on a temporary localhost port with account selection, and by default requests only `https://www.googleapis.com/auth/drive.metadata`. Add `--readonly` for a preview-only `drive.metadata.readonly` token. It saves the token with mode `0600` in a private directory and refuses to overwrite an existing token. Inside a checkout, the output path must be Git-ignored and untracked. It does not run the migration. A Sheets integration would need a separately disclosed Sheets scope; this login command does not request one.

These are restricted account-wide metadata scopes. `drive.file` does not automatically cover arbitrary existing files merely because their IDs are known. Full Drive access is available only through the explicit option below. See Google's [scope guide](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [metadata update API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update), and [Python authorization quickstart](https://developers.google.com/workspace/drive/api/quickstart/python).

## Explicit full-Drive option

The user may separately authorize `https://www.googleapis.com/auth/drive`, which includes access to file contents and management. In the existing Google Cloud project, add this scope under the OAuth consent screen / Google Auth Platform **Data Access** settings if needed. Keep the intended signing-in account listed as a test user while the app is in testing. Reuse the existing Desktop client; no new credentials are required by this code change.

Run this login manually from the checkout, then complete Google consent yourself:

```sh
.venv/bin/python -m uqmc_recovery.google_login \
  --client-json secrets/API_KEY.json \
  --token secrets/token-drive.json --full-drive
```

The existing `secrets/token.json` is preserved. Login refuses to overwrite any token path and saves the new token as mode `0600` under the ignored `secrets/` directory. `--full-drive` and `--readonly` are mutually exclusive; omitting both keeps metadata-only login. Only the exact requested scope is accepted. The transport accepts the explicitly supplied full-Drive token while retaining its exact source-ID, mutation-ID and action guards; it still exposes only metadata operations. It does not start a run or expand the frozen plan. No Sheets scope is requested.

After successful consent, a separate authorized command must explicitly select `--token secrets/token-drive.json`. The currently authorized execution remains the receiving-folder move and the already saved 30-certificate set. Do not run an unrestricted full-plan command merely because a broader token exists. The separately authorized full-scope token subsequently completed the approved folder move and bounded certificate trial.

## Offline validation

The focused suite covers desired-state recovery, combined and existing transports, and local login, including socket timeout wiring and immediate preview progress. It exercises combined updates, existing renamed/moved effects, interruptions around dispatch and readback, lost responses, transient failures, bounded retry, concurrent drift, partial resume, partial rollback/restart, folder dependencies, account/capability checks and private token output.

```sh
PYTHONPATH=.:tests python3 -m unittest \
  test_desired test_desired_parallel test_journal test_desired_transport test_direct_transport -q
```

Tests use fake Drive and temporary evidence. The official Google client serialization test uses a fake HTTP transport. An earlier parallel implementation completed a bounded live trial. The performance-caching changes below are offline-tested only; benchmark speedups are not claims about end-to-end live throughput.


## Per-run validation cache

The desired-state runner owns a deep copy of the frozen plan and calculates its
hash once. CLI scope compilation is performed once. Workers share one validated
section authority for the run, while checking expiry, revocation, new authority
or exclusion events, and evidence-file fingerprints before each dispatch. A
changed binding requires a new validation session; it cannot silently enlarge the
approved scope. Caller changes to the original plan object do not modify the
owned snapshot. The runner never modifies that snapshot.

Journal caching exists only while the existing process writer lock is held. The
initial chain is fully validated, new records are individually validated against
the current sequence/hash, and every append is still fsynced and read back before
its cache entry is installed. Returned records are defensive copies. Unexpected
file replacement, truncation, or modification fails closed. Releasing the writer
lock drops the cache; restart/reacquisition revalidates the chain. Backups retain
full journal and evidence verification. These checks detect accidental or external
changes; the local account owner can still modify files and code.

Source pre-write reads, parent/dependency checks, write-ahead records, readback,
reconciliation, cancellation, and explicit rollback remain in place. Metadata is
not cached across mutation attempts. Journal disk writes remain serialized and
10 workers still have independent HTTP transports. No performance change starts
Drive activity or publishes tracking data.

A bounded synthetic benchmark uses 423 operations, approximately 2.1 MB of plan
JSON and 209 journal records, with no real source inventory or API calls:

```sh
PYTHONPATH=.:tests .venv/bin/python benchmarks/desired_cache.py
```

### Explicit historical version reconciliation

`DesiredRunner.rebaseline_version_only(item, authorization_ref=...)` supports an explicitly reviewed, exact-ID move remaining after a recorded rename. It requires a fresh read with the same known identity, exact name, parents, MIME, content metadata, owners, modified time, ETag and relevant capabilities; only an increased version counter is accepted. The private instruction evidence binds the plan hash and source IDs. It does not confer authority by hashing.

A `DESIRED_REBASELINED` event links the original applied event, unchanged historical before/applied evidence, fresh observed evidence and explicit instruction. No old event is replaced. Restart uses the accepted baseline only for that original event; subsequent version changes and pre-write drift still block. Pending uncertain attempts cannot be rebaselined. This establishes agreement with available evidence, not the cause of a historical version increment. Permission-list omissions are tolerated only in the separately authorized no-audit mode.

### New batches use a fresh spreadsheet snapshot

New CLI `apply` runs require the capture/reconcile/bind workflow in [BATCH_REFRESH.md](BATCH_REFRESH.md). `resume` continues only an already started immutable plan or a fresh bound plan. Reported edits require a pause and explicit refresh, not silent target replacement. Existing exact approval and source pre-write checks remain required.

Folder moves retain their stable IDs at every depth. The runner orders both target-folder dependencies and original containment, including the case where a former child must move out before its old parent can move into it. Independent siblings can still run concurrently. Rollback reverses these dependencies.

Folder membership checks reconcile the frozen descendant graph with verified journal outcomes, including incoming and outgoing subtrees. After related successful operations, a folder counter advance may be accepted only after matching relevant metadata and current membership; a separate `DESIRED_FOLDER_RECONCILED` event preserves the old evidence and links the observations. This does not prove what caused a provider version increment. Unexplained advances, substantive drift and changes after the final pre-write read still block. Pending child successes are read back before parent resume checks, without replaying the write. Preview records no reconciliation events.
