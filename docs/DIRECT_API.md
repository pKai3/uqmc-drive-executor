# Direct Google Drive transport

This transport lets the local executor call Google Drive in a normal Python loop. It keeps the existing frozen plan, stable file IDs, journal, pre-write checks, readbacks and status exports. It does not change operation dependencies or turn an OAuth grant into batch approval. Preview is read-only; applying changes requires a separate explicit command.

The prepared code does not create credentials or start browser authorization. It requires an explicitly supplied, already-authorized local token file. The assistant's connected Drive access is not a reusable credential for this program.

## Proposed scopes

| Purpose | Scope | Boundary |
| --- | --- | --- |
| Read-only preview and reconciliation | `https://www.googleapis.com/auth/drive.metadata.readonly` | Read metadata, including available permission information. |
| Approved metadata renames and parent changes | `https://www.googleapis.com/auth/drive.metadata` | Read and manage metadata across files accessible to the signed-in account. This is a restricted, account-wide metadata scope. |

`drive.metadata` is accepted by both [files.update](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update) and [permissions.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/list). The executor uses only metadata updates: an exact name or exact parent-ID additions/removals. It does not upload/download file bodies, edit sharing, transfer ownership, delete files, or create folders.

The narrower `drive.file` scope covers files opened with or explicitly shared with this particular app. Supplying existing Drive IDs does not itself authorize those files for the app, so this scope is not a drop-in grant for an existing inventory. Google lists both metadata scopes as restricted; applicable consent, testing and verification requirements must be followed. No full `drive` or content-download scope is proposed. See Google's [scope guide](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

## Setup still requiring authorization

No Google setup has been performed. Before a direct API run, the user must explicitly approve the Google account, OAuth application/project and requested scope:

1. Select a Google Cloud project and enable the Drive API. Configure its consent settings and create or supply an appropriate **Desktop app** OAuth client.
2. In a separate, user-authorized setup step, complete Google sign-in and consent for the chosen scope. Google's [Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python) uses the supported `google-auth-oauthlib` desktop flow and a local loopback callback. Do not run that flow implicitly from preview or apply.
3. Keep the resulting authorized-user token and OAuth client configuration in a protected local location, excluded from Git, exports and chat. Limit file permissions to the local user. Pass the token path explicitly; do not paste its contents into commands or messages. A read-only grant cannot apply changes; upgrading to the metadata-write scope requires separate consent.

The transport uses Google's supported Python client and authentication libraries. Install the optional local dependencies with:

```sh
python3 -m pip install -e '.[direct]'
```

Authorization setup and installing dependencies do not start the migration. The default paused state remains paused after setup. Use the existing private plan and store; the paths below are examples, not a request to replace them.

Inspect saved status without a token or network access:

```sh
python3 -m uqmc_recovery.direct status \
  --store private/state --plan private/frozen-plan.json
```

After separate Google authorization, preview or reconcile with read-only API calls:

```sh
python3 -m uqmc_recovery.direct preview \
  --store private/state --plan private/frozen-plan.json \
  --token /absolute/protected/authorized-user-token.json --limit 1

python3 -m uqmc_recovery.direct reconcile \
  --store private/state --plan private/frozen-plan.json \
  --token /absolute/protected/authorized-user-token.json --limit 1
```

Use `--operation-id OPERATION_ID` with `reconcile` to target an unresolved operation. A successful read does not by itself release its recovery fence.

Only after recovery blockers are resolved and execution is separately authorized, apply a bounded first operation:

```sh
python3 -m uqmc_recovery.direct apply --apply \
  --confirm-plan-sha256 FROZEN_PLAN_SHA256 \
  --store private/state --plan private/frozen-plan.json \
  --token /absolute/protected/authorized-user-token.json --limit 1
```

Replace `FROZEN_PLAN_SHA256` with the hash reported for the existing approved plan. To continue the approved programmatic loop, set `--limit` to the desired maximum number of operations, such as `423` for a batch of that size. The count does not expand the approved plan or replay completed operations. Where exports are required, add both `--identity-basis private/identity-basis.json` and `--export-dir private/exports` to the direct command. Keep these outputs private until their intended projection is reviewed for its destination.

## Recovery boundary

Preserve the existing journal and checkpoint before switching transports. Do not create a fresh journal to bypass an uncertain operation. The runner must reconcile any existing unresolved attempt before it can resume. The current legacy connector uncertainty remains fenced even if a new read shows the preimage or target: that read cannot establish the prior tool request's terminal outcome. Direct OAuth authorization alone cannot clear the fence. If terminal evidence is unavailable, retain `UNCERTAIN`, keep dispatch blocked and report it. Do not retry the mutation blindly.

One writer owns the execution store. The transport does not automatically retry writes. It records durable intent before a request and reads the result back afterward. A lost response can still leave an uncertain outcome; there is no exactly-once or atomic compare-and-swap guarantee. Projection/publication failures remain separate from action success, and private evidence must not be included in the public HTML export.
