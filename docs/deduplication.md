# Offline deduplication review

`uqmc_recovery.dedup` produces a private JSON report and CSV review table from an
already exported metadata inventory and optional supplied hashes. It uses only
the Python 3.11+ standard library. It does not contact Drive, open document
bodies, compute hashes from files, load credentials, change permissions, or
dispatch executor actions. There is no execution mode or cleanup command.

The CSV also includes a JSON `metadata` column with MIME, version, captured scope,
provenance and preservation clues for each finding's records. Validation issues
are included as separate rows. Conflicting export sizes or comparable export
checksums suppress the export-match claim and appear as issues.

Every result is a manual-review finding. Even a reliable match of binary bytes
does not establish that two Drive objects serve the same purpose. The report
never chooses a canonical file, selects a deletion candidate, or grants approval
for any change.

## Run the synthetic example

From the repository root:

```sh
python3 -m uqmc_recovery.dedup \
  --inventory examples/dedup/inventory.example.json \
  --ignore-config examples/dedup/ignore.example.json \
  --hashes examples/dedup/hashes.example.json \
  --output-dir private/dedup-demo
```

The output directory receives `report.dedup-report.json` and
`report.dedup-report.csv`. The directory must be entirely new, even if an existing
directory is empty. Omit `--output-dir` to create a unique directory under
`private/dedup-reports/`. Existing reports are not overwritten; use a fresh
output directory for another run. A failed write may leave partial output for
inspection; a retry also needs a fresh directory. Report files are created with
private file permissions. The generated report names are ignored by Git wherever they are
written. CSV cells that could be interpreted as spreadsheet formulas are escaped.
The JSON includes metadata summaries, counts, findings and validation issues.
The CSV includes findings and `validation_issue` rows; it is not a full inventory.

All IDs, titles, scopes, checksums, references, and token fingerprints in
[`examples/dedup/`](../examples/dedup/) are synthetic. The example includes:

- Two binary files with equal SHA-256 and size, retained as distinct audit and
  working objects.
- Legitimately repeated trip-plan titles and equal template fingerprints.
- Native Google files with equal exports and different certified/working roles.
- Shortcuts, a repeated observation of one stable ID, and different IDs with one
  shared link.
- An excluded ID and a shortcut to it, both suppressed from the report.

Run the repository tests with:

```sh
python3 -m unittest discover -s tests -v
```

## Private inputs

Keep real inventory exports, hash manifests, exclusions, journals and reports in
the ignored `private/` directory. Never copy real IDs or private data into the
tracked examples, and never force-add ignored files. Reports contain names,
stable IDs, scopes and provenance; ignoring them prevents ordinary staging, not
disclosure through some other tool.

The inventory must already exist from an authorised export. This script does not
request new access or create an exporter. Normalize metadata into the schema
below without including certificate bodies, record contents, raw text, comments,
or other sensitive document material. The optional hash input is a manifest of
previously supplied evidence, not a path list for scanning files.

### Inventory manifest

The JSON object has `schema_version: 1`, a `provenance` object, a `scope` object,
and a `files` array. See the complete
[synthetic inventory](../examples/dedup/inventory.example.json).

| Field | Meaning |
| --- | --- |
| `provenance.source` | Export source name, used when checking explicit checksum trust. |
| `provenance.reference` | Private reference describing the source of the export. |
| `provenance.captured_at` | ISO capture time of the supplied metadata with an explicit timezone. |
| `scope.label` | Human-readable scope covered by this export. |
| `scope.complete` | Required Boolean stating whether the exporter claims that scope is complete. Missing or malformed values block the report. This does not make it a current live snapshot. |
| `files[].id` | Required stable Drive file ID containing only letters, digits, underscores or hyphens. A name, path, URL or the literal `root` alias is not a substitute. |
| `files[].name` | Required exact exported name, retained for review. |
| `files[].mimeType` | Required MIME type. Classification trims surrounding whitespace and ignores case; all `application/vnd.google-apps.*` objects remain ineligible for binary equivalence. |
| `files[].version` | Nonempty string containing the exported version; required to bind binary and supplied content evidence. |
| `files[].size` | Nonnegative integer byte count, or its decimal string form. Required for a binary checksum match. |
| `files[].md5Checksum`, `sha256Checksum` | Optional original-binary checksum metadata. Accepted only with valid syntax and explicitly trusted provenance. |
| `files[].parents`, `current_scope` | Optional metadata locating the item in the captured scope. These are context, not duplicate proof. |
| `files[].labels` | Optional context labels, such as `audit`, `working`, `certified`, or `repeated-trip-plan`. |
| `files[].modifiedTime` | Optional exported modification-time text retained as context. |
| `files[].provenance` | Optional object with the same three provenance fields, overriding the inventory source for this file. |
| `files[].shortcutDetails.targetId` | Optional shortcut target ID. The script never dereferences it. |
| `files[].webViewLink` | Optional link clue. An equal link with different stable IDs does not establish duplicate files. |
| `files[].review_status` | `withdrawn` or `excluded` suppresses the ID. |
| `files[].in_scope` | `false` excludes an item from this review scope. |

This is a normalized manifest, not a raw Drive API response. Missing size,
version or usable checksum evidence must not be filled with guessed values.
Unknown file-metadata fields are rejected; unknown extra fields in a provenance
object are omitted from the report.
Provenance and capture time are supplied claims; the script cannot authenticate
them or check whether a source has changed since export.

Exactly repeated observations of one stable ID produce an identity-reference
finding. Conflicting observations of the same ID are quarantined with a
validation issue; the script does not choose the first or latest observation.

### Required exclusions and explicit checksum trust

`--ignore-config` is required. It must contain at least one explicit array among
`excluded_ids`, `withdrawn_ids`, and `denied_source_ids`; the union is suppressed.
An intentionally empty array is valid. The existing ignored `private/policy.json`
can be used when it has `denied_source_ids`. Keep permanent withdrawals in that
private policy rather than hardcoding them in source code or examples.

```json
{
  "excluded_ids": [],
  "withdrawn_ids": [],
  "denied_source_ids": [],
  "trusted_checksum_sources": []
}
```

Names in `trusted_checksum_sources` must match the `provenance.source` of an
inventory or hash manifest whose checksum origin you have reviewed. An empty
trust list cannot establish binary equality. A well-formed hexadecimal string
and a source label alone are not evidence of authenticity. The script enforces
the trust assertion supplied here; it cannot verify how a checksum was obtained.
An otherwise valid, version-bound checksum claim from an untrusted source can
still veto binary equality when it contradicts another claim. This conservative
check exposes inconsistent evidence for review. An untrusted matching claim
cannot establish or promote equality.

Exclusions are applied before analysing an item's other metadata. A shortcut to
an excluded target is also suppressed, including a chain of such shortcuts.
Records whose recognized canonical Drive link refers to an excluded target are
suppressed without requesting or following the URL. References to excluded
parents are omitted from retained records and counted as
`omitted_parent_references`. Suppressed records' IDs and names are omitted from
findings and diagnostic details; only exclusion counts are reported. This is
record exclusion, not a general redaction of arbitrary prose in other metadata.
Exclusions do not authorise changes to any other item.

### Optional hashes and text fingerprints

`--hashes` accepts a separate JSON object with `schema_version: 1`, the same
`provenance` fields as the inventory, and a `records` array. Each record binds to
an inventory item with `file_id` and `version`. See the
[synthetic hash manifest](../examples/dedup/hashes.example.json).

Each signal accepts only `file_id`, `version`, `kind`, `algorithm`, and the
kind-specific fields listed below. Extra signal fields, including raw text,
paths and per-signal `provenance`, block the report. Provenance for hash signals
comes only from the manifest's top-level `provenance`. Signals for excluded IDs
or IDs absent from the retained inventory are skipped before their other fields
are analysed.

| `kind` | Evidence fields | Interpretation |
| --- | --- | --- |
| `binary` | `algorithm` (`sha256` or `md5`), hexadecimal `value`, and `size` equal to the inventory's original-binary size | Can support a binary checksum match when provenance is explicitly trusted, versions agree, MIME is non-native, and no conflicting evidence exists. |
| `export` | `algorithm`, hexadecimal `value`, `size`, and `representation` | Equal exports are content candidates only. Compare only the same representation. |
| `text_fingerprint` | `algorithm: "sha256-token-set-v1"`, `representation`, and `token_hashes` | A set of previously supplied token hashes; no raw text is requested or read. |

For fingerprints, `representation` must identify the same extraction,
normalization and tokenization convention for both files. At least eight unique
SHA-256 token hashes are required, and each supplied `token_hashes` array may
contain at most 4,096 entries before duplicates are collapsed. An oversized
fingerprint blocks the report. Similarity is the Jaccard score: shared token
hashes divided by the union of token hashes. The default threshold is `0.85` and
can be changed with `--similarity-threshold`. The report includes the method,
score and evidence counts rather than reproducing token-hash arrays. The threshold
must be greater than zero and at most one. Export and fingerprint provenance
need not appear in the checksum-trust allowlist because these signals can produce
only uncertain content candidates.

Empty or short fingerprints, stale versions, malformed hashes and conflicting
checksums cannot establish equality. A hash manifest containing an excluded ID
does not override the exclusion. Fingerprints can expose information and should
stay private even though they are not plaintext.

## Reading findings

| Category | What it establishes | Remaining uncertainty |
| --- | --- | --- |
| `binary_checksum_match` | Two different stable IDs have equal trusted original-binary checksums and sizes. Evidence distinguishes `strong_sha256` from `legacy_md5`. | MD5 has collision weaknesses. Even SHA-256 is checksum evidence, not a comparison of Drive object histories, permissions, ownership or purpose. |
| `same_name_candidate` | Names match under candidate-search normalization, including a `Copy of` clue. | Equal names, dates, paths, MIME types, and "Original"/"Copy" labels do not prove duplicate content or a preferred original. |
| `content_similar_candidate` | Compatible supplied exports match, or compatible token fingerprints meet the threshold. | Boilerplate, recurring trip plans and different document versions can match. Exports can omit meaningful information. |
| `identity_reference` | Repeated observations identify the same stable ID, or a shortcut relationship refers to a target. | A reference is not an additional independent copy of the target. Conflicting observations of one ID require review. |
| `reference_candidate` | Different stable IDs carry a shared link clue. | Links can be stale, copied, or canonical references. Stable IDs remain distinct. |

Duplicate-comparison findings are pair-level evidence; the script does not infer a
transitive duplicate group from a chain of different checksum algorithms. Each finding contains the
reason, evidence, uncertainty, and a manual-review recommendation. Issues identify
invalid or contradictory evidence so a matching name cannot conceal a checksum
problem.

Native Google Docs, Sheets, Slides, Forms and other Google-native types have no
comparable original binary MD5 in this workflow. Two equal text/PDF exports do
not establish native equivalence. Comments, revision history, spreadsheet
formulas, embedded links, permissions, ownership, signatures and usage context
may differ or be absent from the export.

Preserve audit, working and certified versions and legitimately repeated trip
plans until their distinct purposes have been reviewed. Inspect these roles even
when bytes match. The script does not fetch missing context, recommend an object
to discard, estimate removable space, or prepare any destructive operation.

An incomplete or old inventory can miss matches and differences. Results describe
the supplied snapshot and evidence only; they do not certify the present state
of the Drive or establish that the selected scope contains every relevant item.
An empty findings list is valid and does not certify uniqueness.

The report has these resource bounds:

- At most 4,096 supplied token-hash entries per fingerprint.
- At most 2,000,000 pair examinations per matching pass, including repeated
  examinations of the same pair through different matching buckets.
- At most 250,000 unique candidate pairs per matching pass.
- At most 250,000 findings across the entire report.

Exceeding a bound blocks report generation rather than silently truncating the
results. Narrow the input to a smaller, explicitly reviewed scope and describe
that scope accurately. Separate runs do not compare across their inventories,
so splitting the input can miss matches between them. These bounds limit
comparison expansion; they are not an unlimited-file or unlimited-memory
guarantee.
