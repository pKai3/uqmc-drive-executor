# Desired-state Drive runner

`uqmc_recovery.desired` runs the approved plan through the Google Drive API in a normal Python loop. It does not need an assistant tool call for each file. Focused fake Drive tests cover recovery and rollback; it is not live-tested. Installing or authorizing it does not start execution.

## What one item does

1. Read the exact source ID, name, parent IDs and available safety metadata.
2. Compare the observation with the approved original, intermediate and target states. Stop the item on unapproved drift.
3. Skip effects already satisfied. If both name and parent need changing, use one `files.update` with `body.name`, `addParents` and `removeParents`. Otherwise send only the remaining change.
4. Persist the observed preimage and intended result before dispatch, then read back and record the resulting state.

The scheduler retains real folder dependencies: a receiving folder must reach its required state before an incoming item uses it. It does not require every unrelated rename to finish before moves can start. One local writer holds the journal lock. Other Drive clients are not locked by this process.

Use the existing plan and journal. Checkpoint 508 and the 95 previously verified renames remain intact. A file already bearing its approved name receives only its remaining move; the historical operation order is not a reason to rename it again.

## Resume and conflicts

Current state and request history are separate. A fresh read showing the approved original name is a known current observation, even if an older request returned an ambiguous error. A read showing the target avoids another update; it does not retrospectively prove which request caused that state.

The Google HTTP transport uses a 15-second socket timeout for account, metadata, permission and token-refresh requests. This bounds a blocked connection/read; it is not a strict 15-second wall-clock deadline for an entire response or batch. A response that continues transferring data, multiple network phases, or multiple paginated requests can take longer overall. Interactive OAuth consent keeps its separate five-minute allowance.

Read requests have no hidden client retries. The CLI immediately flushes item start/result lines and safe endpoint/item error context; write failures are reported before reconciliation. Preview honors `--limit` (for example `--limit 1`) and checks Ctrl-C/termination requests between items, retaining completed results. An in-flight read must return or time out before that orderly stop can finish.

Transient write failures have a bounded attempt budget (default three). Before another attempt, read again and recalculate the remaining change. Never blindly replay the previous request. A write timeout has an unknown request outcome; current-state readback decides whether the target is already satisfied, a bounded retry is justified, or the item must stop. If a read is unavailable, an item has unapproved changes, or the remaining outcome is unsafe, stop that item and its dependents with a reason. Independent work need not stop globally.

For a blocked item, inspect the reported observation and reason, resolve the external conflict, then run preview and resume. If the desired change itself has changed, obtain a revised approved plan; do not edit the journal to erase the conflict. The program does not promise exactly-once execution or prove that every timed-out remote request was never processed.

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
  --limit 10 --max-attempts 3 --export-dir private/exports

python3 -m uqmc_recovery.desired resume --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json \
  --limit 10 --max-attempts 3 --export-dir private/exports
```

`--limit` bounds the invocation; it does not expand the approved source set. Export files are local status projections, not spreadsheet writes. Publish the intended status rows in batches, deduplicate log events by Event ID, and preserve human approval cells. A publication failure must not trigger another Drive update. Never publish raw evidence or credentials through the HTML viewer.

The local status format is `uqmc.desired.status.v1`, with explicit coverage limited to items observed during that invocation. It is not a replacement for the existing strict `uqmc.execution.snapshot.v1` workbook/HTML snapshot. The second export uses the existing 26 Operation Log columns; a later sync must map these local results, retain historical evidence and merge by source ID before publishing. No Sheets scope or automatic sync is included.

Preview prints the authenticated account returned by Drive, plus actual source and parent capabilities. No club account is hard-coded. Add `--expect-account YOUR_GOOGLE_EMAIL` to preview/apply/resume/rollback to enforce the account you intend. Explicit capability refusals stop the affected item; omitted capabilities remain unverified. Mutation targets are restricted to the approved manifest source IDs; readable destination or ancestor IDs are not automatically writable targets. Broader OAuth metadata access does not expand this allowlist. See Google's [account read endpoint](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get) and [file capability fields](https://developers.google.com/workspace/drive/api/reference/rest/v3/files).

## Explicit rollback

Rollback is never automatic. It applies only to transitions recorded by this runner, in reverse dependency order. It restores the exact recorded before name and parents only when fresh current metadata matches the recorded applied state. A conflict stops that item rather than overwriting a later manual change. Restarting rollback skips verified restorations.

```sh
python3 -m uqmc_recovery.desired rollback --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token secrets/token.json \
  --limit 10 --max-attempts 3 --export-dir private/exports
```

This command does not automatically undo the 95 historical renames. If an already-renamed item receives only a move, its recorded preimage already contains the approved name; reversing that new transition restores its parent while preserving that name. Target observations requiring no update are not new rollback work. Permission propagation is not restored by a metadata rollback.

## Google authorization remains separate

No credentials or OAuth grant have been created. The assistant's connector session is not a reusable program credential. After explicit user authorization:

1. Choose the Google account and Cloud project, enable Drive API, and configure a Desktop app OAuth client and consent settings.
2. Run the explicit login command below and select your intended Google account in the browser. The project owner and the account granting Drive access can differ; your selected account needs access to the sources and destinations. If the OAuth app is in testing, include that selected account as a test user.
3. Place the downloaded OAuth Desktop client JSON in the ignored local `secrets/API_KEY.json` file. Despite its user-chosen filename, its contents must be OAuth Desktop client configuration, not an API key. Keep it and the resulting token out of Git commits, exports and chat. Pass the token path explicitly. Consent does not authorize automatic execution.

```sh
python3 -m uqmc_recovery.google_login \
  --client-json secrets/API_KEY.json \
  --token secrets/token.json
```

This command is the only entry point that initiates OAuth. It uses Google's installed-app browser flow on a temporary localhost port with account selection, and requests only `https://www.googleapis.com/auth/drive.metadata`. Add `--readonly` for a preview-only `drive.metadata.readonly` token. It saves the token with mode `0600` in a private directory and refuses to overwrite an existing token. Inside a checkout, the output path must be Git-ignored and untracked. It does not run the migration. A Sheets integration would need a separately disclosed Sheets scope; this login command does not request one.

These are restricted account-wide metadata scopes. `drive.file` does not automatically cover arbitrary existing files merely because their IDs are known. No full file-content scope is proposed. See Google's [scope guide](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [metadata update API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update), and [Python authorization quickstart](https://developers.google.com/workspace/drive/api/quickstart/python).

## Offline validation

The focused suite covers desired-state recovery, combined and existing transports, and local login, including socket timeout wiring and immediate preview progress. It exercises combined updates, existing renamed/moved effects, interruptions around dispatch and readback, lost responses, transient failures, bounded retry, concurrent drift, partial resume, partial rollback/restart, folder dependencies, account/capability checks and private token output.

```sh
PYTHONPATH=.:tests python3 -m unittest \
  test_desired test_desired_transport test_direct_transport test_google_login -q
```

Tests use fake Drive and temporary evidence. The official Google client serialization test uses a fake HTTP transport. No production authorization, Drive update, rollback or spreadsheet write was performed for this version. Google authorization and a bounded live preview/test remain necessary before calling it live-tested.
