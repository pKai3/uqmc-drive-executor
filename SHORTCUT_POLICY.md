# Shortcut migration policy

Google Drive shortcuts (`application/vnd.google-apps.shortcut`) are excluded from future migration operations and active completion counts. Titles/extensions do not identify shortcuts. Existing shortcut locations and historical execution evidence remain untouched, including old conflicts and uncertain requests.

Fresh workbook imports use the verified MIME column. Live metadata supplies a second guard. Saved desired-state plans skip shortcut effects; action tickets reject shortcut updates. A folder cannot be moved with a shortcut passenger: destination planning retains that source container and extracts eligible children instead. Explicit rollback also leaves shortcuts in place.

The private journal records `PLAN_EXCLUDED` / `USER_EXCLUDED_SHORTCUT` once per known ID. This is a policy disposition, not deletion or a success result. Private desired status exposes `scope_dispositions`; `active_scope_status` removes excluded IDs from both the denominator and verified numerator while preserving the underlying historical records.

## Publication

Shared snapshots retain historical shortcut effects and add `POLICY_EXCLUDED` / `USER_EXCLUDED_SHORTCUT` reason codes. Sections remove policy-excluded IDs from both active and verified counts. The viewer labels excluded shortcuts explicitly; spreadsheet execution columns show the policy disposition. Neither surface calls them deleted or newly applied. Retired empty folders use a separate verified-trash disposition and are hidden only when no active/protected descendants remain. All original identities and journal history remain available.

No Drive permission listing, sharing edit, ownership transfer, shortcut deletion or reversal is part of this policy.
