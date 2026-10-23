# Shortcut migration policy

Google Drive shortcuts (`application/vnd.google-apps.shortcut`) are excluded from future migration operations and active completion counts. Titles/extensions do not identify shortcuts. Existing shortcut locations and historical execution evidence remain untouched, including old conflicts and uncertain requests.

Fresh workbook imports use the verified MIME column. Live metadata supplies a second guard. Saved desired-state plans skip shortcut effects; action tickets reject shortcut updates. A folder cannot be moved with a shortcut passenger: destination planning retains that source container and extracts eligible children instead. Explicit rollback also leaves shortcuts in place.

The private journal records `PLAN_EXCLUDED` / `USER_EXCLUDED_SHORTCUT` once per known ID. This is a policy disposition, not deletion or a success result. Private desired status exposes `scope_dispositions`; `active_scope_status` removes excluded IDs from both the denominator and verified numerator while preserving the underlying historical records.

## Publication limitation

The current shared viewer contract supports confirmed-deletion retirement, but has no policy-exclusion disposition. Shortcuts must not be called deleted or marked newly applied. Until that contract and viewer support are authorized and implemented, publication stops before writes with `SHORTCUT_POLICY_PUBLICATION_REQUIRES_SUPPORTED_DISPOSITION`. Saved-publication retry through the destination CLI is also held when policy exclusions are recorded. Local migration and recovery records remain available; no existing public artifact is silently altered.

No Drive permission listing, sharing edit, ownership transfer, shortcut deletion or reversal is part of this policy.
