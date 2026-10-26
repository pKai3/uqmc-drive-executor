#!/usr/bin/env python3
"""Refresh an existing UQMC viewer from a verified, redacted publication bundle.

No network calls. The existing source identity set is an immutable disclosure
allowlist. Destination links come only from the verified sections envelope.
"""
import argparse
import base64
import copy
import gzip
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

TREE = re.compile(r'(<script id="tree-data"[^>]*>)(.*?)(</script>)', re.S)
START, END = '<!-- UQMC-MIGRATION-STATUS-START -->', '<!-- UQMC-MIGRATION-STATUS-END -->'


def unpack(source):
    m = TREE.search(source)
    if not m:
        raise ValueError('TREE_DATA_MISSING')
    return json.loads(gzip.decompress(base64.b64decode(m.group(2))))


def folder_link(value):
    parsed = urlparse(value or '')
    if parsed.scheme != 'https' or parsed.netloc != 'drive.google.com' or not parsed.path.startswith('/drive/folders/') or parsed.query or parsed.fragment:
        raise ValueError('UNSAFE_DESTINATION_LINK')
    return value


def path_parts(value):
    return tuple(x.strip() for x in value.split('/') if x.strip())


def resolve_folder_path(data, value):
    """Resolve canonical collection aliases without renaming/reparenting nodes."""
    parent = -1
    node = None
    for segment in path_parts(value):
        matches = [n for n in data['nodes'] if n['p'] == parent and n['s'] == 'destination'
                   and n['f'] and not n.get('viewer_hidden')
                   and segment in {n['n'].strip(), (n.get('o') or '').strip()}]
        if len(matches) != 1:
            raise ValueError('SECTION_PATH_NOT_UNIQUE:' + value)
        node = matches[0]
        parent = node['i']
    if node is None:
        raise ValueError('SECTION_PATH_NOT_UNIQUE:' + value)
    return node


def register_proposal_files(data, workbook, identity_basis):
    """Materialize only explicitly approved additions to the viewer identity basis.

    Being present in a spreadsheet is not disclosure approval. Each new identity
    needs a private registration bound to its stable ID, rename row and original
    title. Existing disclosed nodes keep their history; refresh_proposal handles
    current targets and rebuilds tree ordering/counts after this function.
    """
    from .projection import _identity_coverage
    _identity_coverage(identity_basis, identity_basis['source_ids'])
    registrations = identity_basis.get('viewer_registrations', [])
    allowed = set(identity_basis['source_ids'])
    rows = workbook.get('sheets', workbook.get('matrices', {}))['Proposed Renames']
    records = [dict(zip(rows[0], row)) for row in rows[1:]]
    seen, added = set(), []
    for registration in registrations:
        sid = registration.get('source_id')
        required = ('source_id', 'rename_id', 'original_name', 'current_display_name', 'approval_reference')
        if (any(not isinstance(registration.get(k), str) or not registration[k] for k in required)
                or sid not in allowed or sid in seen):
            raise ValueError('VIEWER_REGISTRATION_INVALID')
        seen.add(sid)
        existing = [n for n in data['nodes'] if n.get('id') == sid]
        if existing:
            if len(existing) != 1 or existing[0]['f'] or existing[0]['s'] != 'destination':
                raise ValueError('VIEWER_REGISTRATION_ID_CONFLICT')
            continue
        matches = [r for r in records if r.get('Source ID') == sid]
        if len(matches) != 1:
            raise ValueError('VIEWER_REGISTRATION_ROW_NOT_UNIQUE')
        row = matches[0]
        if (row.get('Rename ID') != registration['rename_id']
                or row.get('Current name + type suffix') != registration['current_display_name']
                or not row.get('Proposed name + type suffix')):
            raise ValueError('VIEWER_REGISTRATION_ROW_MISMATCH')
        parent = resolve_folder_path(data, row['Proposed destination folder'])
        data['nodes'].append(dict(i=len(data['nodes']), p=parent['i'], s='destination',
            n=row['Proposed name + type suffix'], o=registration['original_name'],
            f=False, id=sid, u='', context_id='', t=[registration['rename_id']],
            note='', rank='', changed=True, review=False, shortcut=False,
            assessment='Proposed rename', assistant_pending=False,
            requires_user_naming_decision=False, keep_basis=None, necessity_rationale='',
            cleanup=None, archive_exempt=False, duplicate=None, collision=None, c=[],
            total=0, changedTotal=1, reviewTotal=0))
        added.append(sid)
    return added


def node_paths(data):
    """Resolve parent links by node identity, including appended proposal folders."""
    nodes={n['i']:n for n in data['nodes']}
    if len(nodes)!=len(data['nodes']):raise ValueError('DUPLICATE_VIEWER_NODE_ID')
    paths={};visiting=set()
    def path(identity):
        if identity in paths:return paths[identity]
        if identity in visiting:raise ValueError('PROPOSAL_TREE_CYCLE')
        if identity not in nodes:raise ValueError('PROPOSAL_PARENT_MISSING')
        visiting.add(identity);n=nodes[identity]
        paths[identity]=(path(n['p']) if n['p']>=0 else ())+(n['n'].strip(),)
        visiting.remove(identity)
        return paths[identity]
    for identity in nodes:path(identity)
    return paths


def retained_proposal_containers(data, workbook):
    """Keep explicit source-only wrappers as audit context, not destinations.

    A retain row must identify the same original source path as its target.
    Any accompanying rename row must retain the original source location.
    Existing display aliases never become authority to move the source wrapper.
    """
    matrices = workbook.get('sheets', workbook.get('matrices', {}))
    manifest = matrices.get('Move Manifest', [])
    retained = {}
    destination_ids = {n.get('id') for n in data['nodes']
                       if n['f'] and n['s'] == 'destination'}
    for row in manifest[1:]:
        record = dict(zip(manifest[0], row))
        if record.get('Scope', '').strip().casefold() != 'retain container only':
            continue
        sid, source = record.get('Source ID'), record.get('Source path')
        if not sid or sid not in destination_ids or not source:
            continue
        if record.get('Exact target path') != source:
            raise ValueError('RETAINED_CONTAINER_TARGET_CHANGED:' + sid)
        if sid in retained and retained[sid] != source:
            raise ValueError('AMBIGUOUS_RETAINED_CONTAINER:' + sid)
        retained[sid] = source
    rows = matrices['Proposed Renames']
    for row in rows[1:]:
        record = dict(zip(rows[0], row)); sid = record.get('Source ID')
        if sid not in retained:
            continue
        current = record.get('Current name + type suffix')
        proposed = record.get('Proposed name + type suffix')
        parent = record.get('Proposed destination folder')
        if (not current or not proposed or not parent
                or parent.rstrip('/') + '/' + current != retained[sid]):
            raise ValueError('RETAINED_CONTAINER_RENAME_CONFLICT:' + sid)
    for n in data['nodes']:
        sid = n.get('id')
        if (n['f'] and n['s'] == 'destination' and sid in retained
                and not n.get('destination_overlay')):
            n['retained_source_container'] = {'source_path': retained[sid]}
            n['viewer_hidden'] = True
        elif n.pop('retained_source_container', None):
            n.pop('viewer_hidden', None)
    return retained


def proposed_folder_updates(data, workbook, *, folder_revisions=()):
    """Map display-only folder revisions from current sheet targets and stable IDs.

    A known created container can follow its children only when every disclosed
    file's explicit sheet target agrees with the same subtree transformation.
    This does not freeze a migration plan or confer execution approval.
    """
    retained = retained_proposal_containers(data, workbook)
    nodes = data['nodes']; paths = node_paths(data); children = {}
    for n in nodes:
        children.setdefault(n['p'], []).append(n)
    rows = workbook.get('sheets', workbook.get('matrices', {}))['Proposed Renames']
    ix = {k: rows[0].index(k) for k in ('Source ID', 'Proposed name + type suffix', 'Proposed destination folder')}
    targets = {}
    for row in rows[1:]:
        if len(row) <= max(ix.values()): continue
        sid, name, parent = (row[ix[k]] for k in ix)
        if not sid or not name or not parent: continue
        if sid in targets: raise ValueError('DUPLICATE_PROPOSAL_SOURCE')
        targets[sid] = (parent.rstrip('/') + '/' + name, parent, name)
    # Keep-name folder moves have an explicit manifest mapping but need no
    # rename row. Use that stable-ID mapping for display, never infer a move
    # merely from children or introduce an undisclosed source identity.
    from .importer import subtree_coverage_scope
    matrices = workbook.get('sheets', workbook.get('matrices', {}))
    manifest = matrices.get('Move Manifest', [])
    known_folders = {n['id']: n for n in nodes
                     if n['f'] and n['s'] == 'destination' and n.get('id')}
    manifest_targets = {}
    for row in manifest[1:]:
        record = dict(zip(manifest[0], row))
        sid = record.get('Source ID')
        n = known_folders.get(sid)
        if n is None or sid in targets or not subtree_coverage_scope(record.get('Scope')):
            continue
        path = record.get('Exact target path')
        name = n['n']
        if not isinstance(path, str) or not path.endswith('/' + name):
            raise ValueError('MANIFEST_FOLDER_TARGET_NAME_MISMATCH:' + sid)
        target = (path, path[:-len('/' + name)], name)
        if sid in manifest_targets and manifest_targets[sid] != target:
            raise ValueError('AMBIGUOUS_MANIFEST_FOLDER_TARGET:' + sid)
        manifest_targets[sid] = target
    frozen = {r['source_id']: r for r in folder_revisions}
    result = {}
    for n in nodes:
        if not n['f'] or n['s'] != 'destination' or n.get('viewer_hidden'): continue
        sid = n.get('destination_overlay', {}).get('destination_id') or n.get('id')
        if not sid: continue
        current = '/'.join(paths[n['i']])
        target = targets.get(n.get('id')) or manifest_targets.get(n.get('id'))
        if target:
            path, _, name = target
        elif n.get('destination_overlay'):
            direct = [c for c in children.get(n['i'], []) if not c['f'] and not c.get('retired') and not c.get('policy_excluded')]
            if not direct or any(c.get('id') not in targets for c in direct): continue
            parents = {targets[c['id']][1] for c in direct}
            if len(parents) != 1: continue
            path = next(iter(parents)); name = path.rsplit('/', 1)[-1]
            prefix = paths[n['i']]
            descendants = [c for c in nodes if not c['f'] and not c.get('retired') and not c.get('policy_excluded')
                           and paths[c['i']][:len(prefix)] == prefix]
            if any(c.get('id') not in targets or path_parts(targets[c['id']][1]) !=
                   path_parts(path) + paths[c['i']][len(prefix):-1] for c in descendants): continue
        else: continue
        if path_parts(path) == paths[n['i']] and not n.get('pending_revision'): continue
        try:
            same = resolve_folder_path(data, path) is n
        except ValueError:
            same = False
        prior_pending = n.get('pending_revision', {})
        if prior_pending.get('inherited'): prior_pending = {}
        if same and not prior_pending and (not target or name == n['n']): continue
        before = prior_pending.get('before_path', current)
        if path_parts(path) == path_parts(current) and not prior_pending: continue
        previous = frozen.get(sid)
        verified = bool(previous and previous['verified'] and
                        path_parts(previous['target_path']) == path_parts(path))
        result[sid] = dict(source_id=sid, before_path=before, target_path=path,
                           target_name=name, verified=verified)
    for sid, r in frozen.items():
        if sid not in result and sid not in retained: result[sid] = r
    ordered = sorted(result.values(), key=lambda r: len(path_parts(r['before_path'])))
    independent = []
    for r in ordered:
        inherited = any(r['before_path'].startswith(p['before_path'] + '/') and
            r['target_path'] == p['target_path'] + r['before_path'][len(p['before_path']):]
            for p in independent)
        if not inherited: independent.append(r)
    return independent


def mark_proposed_revision_status(data, revisions):
    """Keep historical observations separate from satisfaction of fresh targets."""
    pending = {r['source_id']: r for r in revisions if not r['verified']}
    pending_members = set(); counts = {}
    historical = {sid for sec in data['execution_sections']['sections'] for sid in sec.get('verified_file_ids', [])}
    for n in data['nodes']:
        sid = n.get('destination_overlay', {}).get('destination_id') or n.get('id')
        inherited = data['nodes'][n['p']].get('pending_revision') if n['p'] >= 0 else None
        if sid in pending:
            r = pending[sid]
            n['pending_revision'] = {k:r[k] for k in ('before_path', 'target_path', 'target_name')}
        elif inherited:
            n['pending_revision'] = {**inherited, 'inherited': True}
        else: n.pop('pending_revision', None)
        active = not n['f'] and not n.get('retired') and not n.get('policy_excluded')
        if active and n.get('pending_revision'): pending_members.add(n['id'])
        counts[n['i']] = [int(active), int(active and n.get('id') in historical),
                          int(active and n.get('id') in pending_members), int(not n['f'] and bool(n.get('retired')))]
    for n in reversed(data['nodes']):
        if n['p'] >= 0:
            for k in range(4): counts[n['p']][k] += counts[n['i']][k]
        if n['f']:
            total, done, pending_count, retired = counts[n['i']]
            summary = n.setdefault('migration_summary', {})
            old_total = summary.get('file_count', 0)
            summary.update(file_count=total, historically_completed_file_count=done,
                           completed_file_count=max(0, done - pending_count), retired_file_count=retired)
            if pending_count:
                summary.update(status='REVISION_PENDING', pending_revision_file_count=pending_count)
            else:
                summary.pop('pending_revision_file_count', None)
                if not total and (old_total or summary.get('status') == 'CURRENT_CONTAINER_RETAINED') and n.get('destination_overlay'):
                    summary['status'] = 'CURRENT_CONTAINER_RETAINED'
                elif total and done == total: summary['status'] = 'COMPLETE'
                elif done: summary['status'] = 'PARTIAL'
                elif total: summary['status'] = 'NOT_STARTED'
    for sec in data['execution_sections']['sections']:
        root = resolve_folder_path(data, sec['path'])
        count = counts[root['i']][2]
        if count:
            sec['proposal_status'] = 'PENDING_REVISION'
            sec['proposal_pending_file_count'] = count
        else:
            sec.pop('proposal_status', None); sec.pop('proposal_pending_file_count', None)
    data['proposal_revision_summary'] = {**data.get('proposal_revision_summary', {}), 'pending_folder_count': len(pending),
        'pending_file_count': len(pending_members), 'basis': 'current_sheet_display_only'}


def refresh_proposal(data, workbook, *, folder_revisions=(), proposed_layout=False):
    """Reconcile explicit rename rows, recognizing already-renamed folder aliases.

    Stable source IDs and source links never change. Unlisted/withheld identities
    are not introduced. The existing file tree is retained for records without
    a proposed rename row; nothing is inferred from absent spreadsheet rows.
    """
    retained = retained_proposal_containers(data, workbook)
    nodes = data['nodes']
    by_id = {n['id']: n for n in nodes if n.get('id')}
    rows = workbook.get('sheets', workbook.get('matrices', {}))['Proposed Renames']
    header = rows[0]
    ix = {k: header.index(k) for k in ['Source ID', 'Proposed name + type suffix', 'Proposed destination folder']}
    changed = []
    created = []

    def resolve(parts):
        parent = -1
        for part in parts:
            candidates = [n for n in nodes if n['p'] == parent and n['s'] == 'destination' and n['f'] and not n.get('viewer_hidden') and part in {n['n'].strip(), (n.get('o') or '').strip()}]
            if len(candidates) > 1:
                raise ValueError('AMBIGUOUS_PROPOSAL_FOLDER:' + '/'.join(parts))
            if candidates:
                parent = candidates[0]['i']
                continue
            node = dict(i=len(nodes), p=parent, s='destination', n=part, o=None, f=True,
                        id='', u='', context_id='', t=[], note='', rank='', changed=False,
                        review=False, shortcut=False, assessment=None, assistant_pending=False,
                        requires_user_naming_decision=None, keep_basis=None, necessity_rationale='',
                        cleanup=None, archive_exempt=False, duplicate=None, collision=None, c=[],
                        total=0, changedTotal=0, reviewTotal=0)
            nodes.append(node)
            created.append(node['i'])
            parent = node['i']
        return parent

    # A completed folder revision reuses the same viewer node and descendants.
    # Pending revisions retain the current tree and expose their proposed path.
    revisions = {r['source_id']: r for r in folder_revisions}
    if len(revisions) != len(folder_revisions):
        raise ValueError('DUPLICATE_VIEWER_FOLDER_REVISION')
    for revision in folder_revisions:
        sid = revision['source_id']
        matches = [n for n in nodes if n['f'] and n['s'] == 'destination' and
                   (n.get('id') == sid or n.get('destination_overlay', {}).get('destination_id') == sid)]
        if len(matches) != 1:
            raise ValueError('REVISION_VIEWER_ID_NOT_UNIQUE:' + sid)
        n = matches[0]
        current = None
        for path in (revision['before_path'], revision['target_path']):
            try:
                candidate = resolve_folder_path(data, path)
            except ValueError:
                continue
            if candidate is n:
                current = candidate
                break
        if current is None:
            raise ValueError('REVISION_VIEWER_PATH_ID_MISMATCH:' + sid)
        if revision['verified'] or proposed_layout:
            parent_path = revision['target_path'][:-len('/' + revision['target_name'])]
            parent = resolve(path_parts(parent_path))
            collision = [other for other in nodes if other is not n and other['p'] == parent
                         and other['f'] and other['s'] == 'destination' and not other.get('viewer_hidden')
                         and other['n'] == revision['target_name']]
            if collision:
                # A corrected proposal may already show an anonymous target
                # context. Reuse its display children under the stable-ID node;
                # a real folder identity at that path remains a hard conflict.
                if (len(collision) != 1 or any(collision[0].get(k)
                        for k in ('id', 'context_id', 'destination_overlay'))):
                    raise ValueError('REVISION_VIEWER_TARGET_COLLISION:' + sid)
                placeholder = collision[0]
                def anonymous(node):
                    return not any(node.get(k) for k in ('id', 'context_id', 'destination_overlay'))
                def adopt_context(context, target):
                    for child in [c for c in nodes if c['p'] == context['i']]:
                        peers = [c for c in nodes if c['p'] == target['i'] and c['f'] and child['f']
                                 and not c.get('viewer_hidden') and not child.get('viewer_hidden')
                                 and c['n'] == child['n']]
                        if len(peers) > 1:
                            raise ValueError('REVISION_VIEWER_CHILD_COLLISION:' + sid)
                        if not peers:
                            child['p'] = target['i']
                        elif anonymous(child):
                            adopt_context(child, peers[0])
                        elif anonymous(peers[0]):
                            adopt_context(peers[0], child)
                            child['p'] = target['i']
                        else:
                            raise ValueError('REVISION_VIEWER_CHILD_COLLISION:' + sid)
                    context['viewer_hidden'] = True
                    context['proposal_context_replaced_by'] = sid
                adopt_context(placeholder, n)
            if n['p'] != parent or n['n'] != revision['target_name']:
                changed.append(sid)
            n['p'], n['n'] = parent, revision['target_name']
            n.pop('pending_revision', None)
            if not revision['verified']:
                n['pending_revision'] = {k:revision[k] for k in ('before_path','target_path','target_name')}
        else:
            # Freeze can create target parent containers before dispatch. Keep
            # the source node in place while allowing their verified overlays.
            resolve(path_parts(revision['target_path'][:-len('/' + revision['target_name'])]))
            n['pending_revision'] = {k:revision[k] for k in ('before_path','target_name','target_path')}

    def current_proposal_path(path):
        for revision in sorted(folder_revisions, key=lambda x: len(x['target_path']), reverse=True):
            target = revision['target_path']
            if not proposed_layout and not revision['verified'] and (path == target or path.startswith(target + '/')):
                return revision['before_path'] + path[len(target):]
        return path

    seen = set()
    for row in rows[1:]:
        if len(row) <= max(ix.values()):
            continue
        sid = row[ix['Source ID']]
        if not sid or sid not in by_id:
            continue
        if sid in seen:
            raise ValueError('DUPLICATE_PROPOSAL_SOURCE')
        seen.add(sid)
        n = by_id[sid]
        target_name = row[ix['Proposed name + type suffix']]
        if not isinstance(target_name, str) or not target_name:
            raise ValueError('INVALID_PROPOSED_NAME')
        # Folder aliases are already rendered with their proposed name. Avoid
        # silently moving their whole subtree based on a file-level row refresh.
        if n['f']:
            if sid in retained:
                continue
            if n['n'] != target_name:
                revision = revisions.get(sid)
                if revision is None or revision['verified'] or revision['target_name'] != target_name:
                    raise ValueError('FOLDER_PROPOSAL_CHANGED_REQUIRES_REBUILD')
            continue
        parent = n['p']
        if n['s'] == 'destination':
            parent = resolve(path_parts(current_proposal_path(row[ix['Proposed destination folder']])))
        if parent != n['p'] or target_name != n['n']:
            changed.append(sid)
            n['p'] = parent
            n['n'] = target_name
            n['changed'] = True
        if 'Necessity rationale (assistant)' in header:
            pos = header.index('Necessity rationale (assistant)')
            if len(row) > pos:
                n['necessity_rationale'] = row[pos]

    # Restore preorder; renderer's aggregation relies on children following parents.
    children = {n['i']: [] for n in nodes}
    roots = {'destination': [], 'retained': []}
    for n in nodes:
        if n['p'] < 0:
            roots[n['s']].append(n['i'])
        else:
            children[n['p']].append(n['i'])
    order = []
    def visit(i):
        order.append(i)
        for c in children[i]:
            visit(c)
    for section in ['destination', 'retained']:
        for i in roots[section]:
            visit(i)
    if len(order) != len(nodes) or len(set(order)) != len(nodes):
        raise ValueError('PROPOSAL_TREE_CYCLE')
    index = {old: new for new, old in enumerate(order)}
    result = []
    for old in order:
        n = nodes[old]
        n['i'] = index[old]
        n['p'] = index[n['p']] if n['p'] >= 0 else -1
        n['c'] = [index[x] for x in children[old]]
        n['total'] = 0
        n['changedTotal'] = int(bool(n.get('changed')))
        n['reviewTotal'] = int(bool(n.get('review')))
        result.append(n)
    for n in reversed(result):
        if n['p'] >= 0:
            p = result[n['p']]
            p['total'] += 1 + n['total']
            p['changedTotal'] += n['changedTotal']
            p['reviewTotal'] += n['reviewTotal']
    data['nodes'] = result
    data['roots'] = {k: [index[i] for i in v] for k, v in roots.items()}
    data['verified'] = workbook['exported_at_utc']
    return {'proposal_records_updated': len(changed), 'proposal_contexts_added': len(created)}


def integrate(data, bundle):
    public = bundle['html']
    # The companion envelope is intentionally separate from strict snapshot.v1.
    # Copy only the public section fields, never arbitrary transport metadata.
    section_fields = {'section_id','title','path','destination_id','destination_url','mode',
        'expected_file_count','completed_file_count','remaining_file_count','status',
        'retained_source_folder_ids','created_folder_ids','created_folder_count',
        'created_destinations','rename_effects','direct_move_effects','inherited_locations',
        'source_folder_status_basis','verified_at_utc','verified_file_ids','verified_destinations',
        'retired_file_ids','retired_file_count','policy_excluded_file_ids','policy_excluded_file_count',
        'pending_destination_ids'}
    destination_fields = {'id','name','parent_ids','verified_at_utc','destination_link','path','mode'}
    sections = {k: copy.deepcopy(v) for k,v in bundle['sections'].items()
                if k in {'schema_version','snapshot_id','generated_at_utc','journal_sequence',
                         'journal_tip_sha256','basis','other_sections'}}
    sections['retired_empty_folders'] = [{k:proof[k] for k in ('source_id','verified_at_utc','reason_code')}
        for proof in bundle['sections'].get('retired_empty_folders', [])]
    sections['sections'] = [{k: copy.deepcopy(v) for k,v in s.items() if k in section_fields}
                            for s in bundle['sections']['sections']]
    for section in sections['sections']:
        section['created_destinations'] = [{k:v for k,v in d.items() if k in destination_fields}
                                           for d in section.get('created_destinations',[])]
        section['verified_destinations'] = [{k:v for k,v in d.items() if k in destination_fields}
                                           for d in section.get('verified_destinations',[])]
    nodes = data['nodes']
    ids = {n['id'] for n in nodes if n.get('id')}
    items = {x['source_id']: x for x in public['items']}
    verified_items = {x['source_id']: x for x in bundle.get('workbook', {}).get('items', [])}
    if len(items) != len(public['items']) or set(items) != ids:
        raise ValueError('PUBLIC_IDENTITY_ALLOWLIST_MISMATCH')
    if public['snapshot_id'] != sections['snapshot_id'] or public['journal_sequence'] != sections['journal_sequence']:
        raise ValueError('PUBLICATION_SNAPSHOT_MISMATCH')
    # Explicit rejection of raw observations/evidence in a public payload.
    for item in items.values():
        if any(item['observed'].get(k) is not None for k in ('exact_name', 'parent_ids', 'mime_type')) or item['location'].get('verified_path') is not None:
            raise ValueError('PRIVATE_OBSERVATION_IN_PUBLIC_PAYLOAD')
    paths = {}
    for n in nodes:
        paths[n['i']] = (paths[n['p']] if n['p'] >= 0 else ()) + (n['n'].strip(),)
    path_nodes = {}
    for n in nodes:
        if n['s'] == 'destination' and n['f']:
            path_nodes.setdefault(paths[n['i']], []).append(n)
    retired_files = {sid for sid, item in items.items() if
        {'RETIRED', 'USER_CONFIRMED_DELETED', 'NOT_APPLIED'} <= set(item.get('reason_codes', []))}
    policy_files = {sid for sid,item in items.items() if
        {'POLICY_EXCLUDED','USER_EXCLUDED_SHORTCUT'} <= set(item.get('reason_codes', []))}
    for n in nodes:
        # Rebuilt only from the current journal projection; no stale HTML flag.
        n.pop('retired', None)
        n.pop('policy_excluded', None)
        if not n['f'] and n.get('id') in policy_files:
            n['policy_excluded'] = True
        if not n['f'] and n.get('id') in retired_files:
            n['retired'] = True
    completed_files = set()
    pending_folders = set()
    known_folder_ids = {n.get('id') for n in nodes if n['f'] and n.get('id')}
    retained = set()
    for section in sections['sections']:
        root = resolve_folder_path(data, section['path'])
        path = paths[root['i']]
        root_url = folder_link(section['destination_url'])
        members = [n for n in nodes if n['s'] == 'destination' and not n['f'] and paths[n['i']][:len(path)] == path]
        retired = section.get('retired_file_ids', [])
        expected_retired = {n['id'] for n in members} & retired_files
        if (len(retired) != len(set(retired)) or set(retired) != expected_retired or
                section.get('retired_file_count', 0) != len(retired)):
            raise ValueError('SECTION_RETIRED_MEMBERSHIP_MISMATCH')
        excluded = section.get('policy_excluded_file_ids', [])
        expected_excluded = {n['id'] for n in members} & policy_files
        if (len(excluded) != len(set(excluded)) or set(excluded) != expected_excluded or
                section.get('policy_excluded_file_count', 0) != len(excluded)):
            raise ValueError('SECTION_POLICY_EXCLUSION_MEMBERSHIP_MISMATCH')
        members = [n for n in members if n['id'] not in retired_files | policy_files]
        if len(members) != section['expected_file_count']:
            raise ValueError('SECTION_MEMBERSHIP_COUNT_MISMATCH:' + section['section_id'])
        if section['status'] == 'COMPLETED' and section['completed_file_count'] != len(members):
            raise ValueError('SECTION_COMPLETION_COUNT_MISMATCH')
        verified = section.get('verified_file_ids',
            [n['id'] for n in members] if section['status'] == 'COMPLETED' else [])
        if (len(verified) != len(set(verified)) or not set(verified) <= {n['id'] for n in members}
                or len(verified) != section['completed_file_count']):
            raise ValueError('SECTION_VERIFIED_MEMBERSHIP_MISMATCH')
        destination_ids = {section['destination_id']} | {d['id'] for d in section.get('created_destinations', []) + section.get('verified_destinations', [])}
        for member in members:
            if member['id'] not in verified:
                continue
            observed_item = verified_items.get(member['id'], {})
            observed = observed_item.get('observed', {})
            parents = observed.get('parent_ids')
            location = observed_item.get('location', {}).get('effect')
            proof = (member['id'] in section['verified_file_ids'] if 'verified_file_ids' in section else
                observed_item.get('rename', {}).get('effect') == 'APPLIED' and
                (observed_item.get('move', {}).get('effect') == 'APPLIED' or location in {'INHERITED', 'DIRECT_AND_INHERITED'}))
            if (observed.get('read_state') != 'VERIFIED' or not parents or
                    not set(parents) <= destination_ids or not proof):
                raise ValueError('SECTION_MEMBER_NOT_VERIFIED_AT_DESTINATION')
        completed_files.update(verified)
        retained.update(section.get('retained_source_folder_ids', []))
        destinations = {x['id']: x for x in section.get('created_destinations', [])}
        destinations.update({x['id']: x for x in section.get('verified_destinations', [])})
        pending = section.get('pending_destination_ids', [])
        if (len(pending) != len(set(pending)) or
                not set(pending) <= known_folder_ids | set(destinations) or
                (pending and section['status'] == 'COMPLETED')):
            raise ValueError('INVALID_PENDING_DESTINATION_PROOF')
        # Creation history is retained even if a later parent rename/move is
        # pending. Only target-satisfied chains enter the verified location graph.
        pending_folders.update(pending)
        destinations = {sid: d for sid, d in destinations.items() if sid not in pending}
        destination_paths = {section['destination_id']: path}
        unresolved = set(destinations) - set(destination_paths)
        while unresolved:
            progress = False
            for fid in list(unresolved):
                d = destinations[fid]
                parents = d['parent_ids']
                if len(parents) == 1 and parents[0] in destination_paths:
                    destination_paths[fid] = destination_paths[parents[0]] + (d['name'].strip(),)
                    unresolved.remove(fid)
                    progress = True
            if not progress:
                raise ValueError('CREATED_DESTINATION_GRAPH_INCOMPLETE')
        root['destination_overlay'] = {'destination_id': section['destination_id'], 'destination_url': root_url, 'mode': section['mode'], 'section_id': section['section_id']}
        for fid, dp in destination_paths.items():
            if fid == section['destination_id']:
                continue
            # A verified revision preserves destination ID while changing its
            # logical path. Prefer that existing stable identity over stale aliases.
            matches = [n for n in nodes if n['s']=='destination' and n['f'] and
                       (n.get('id')==fid or n.get('destination_overlay',{}).get('destination_id')==fid)]
            if not matches:
                matches = path_nodes.get(dp, [])
            if len(matches) != 1:
                raise ValueError('CREATED_DESTINATION_PATH_NOT_UNIQUE:' + '/'.join(dp))
            d = destinations[fid]
            matches[0]['destination_overlay'] = {'destination_id': fid, 'destination_url': folder_link(d['destination_link']), 'mode': d.get('mode','CREATED'), 'section_id': section['section_id']}

    counts = {n['i']: [int(not n['f'] and n.get('id') not in retired_files | policy_files),
        int(not n['f'] and n.get('id') in completed_files),
        int(not n['f'] and n.get('id') in retired_files),
        int(not n['f'] and n.get('id') in policy_files)] for n in nodes}
    for n in reversed(nodes):
        if n['p'] >= 0:
            for k in (0, 1, 2, 3):
                counts[n['p']][k] += counts[n['i']][k]
    for n in nodes:
        if not n['f']:
            continue
        total, done, retired_count, excluded_count = counts[n['i']]
        if n['s'] == 'retained':
            state = 'SOURCE_RETAINED' if n.get('id') in retained else 'NOT_TRACKED'
        elif (n.get('destination_overlay', {}).get('destination_id') or n.get('id')) in pending_folders:
            state = 'DESTINATION_PENDING'
        elif done and done == total or (not total and n.get('destination_overlay')):
            state = 'COMPLETE'
        elif done:
            state = 'PARTIAL'
        else:
            state = 'NOT_STARTED'
        n['migration_summary'] = {'status': state, 'file_count': total, 'completed_file_count': done, 'retired_file_count': retired_count, 'policy_excluded_file_count': excluded_count, 'source_retained': n.get('id') in retained}
    data['execution'] = public
    data['execution_sections'] = sections
    mark_retired_empty_folders(data)
    data['notes'] = [re.sub(r'This remains a proposal; no source moves or renames have been executed\.', 'The tree combines the current proposal with the separately verified execution snapshot below; untracked records remain proposals.', x) for x in data['notes']]


def mark_retired_empty_folders(data, workbook=None):
    """Hide only proved retired branches with no active or protected descendants."""
    nodes = data['nodes']; sections = data.get('execution_sections', {})
    proofs = sections.get('retired_empty_folders', [])
    if any(p.get('reason_code') != 'VERIFIED_EMPTY_PARENT_TRASHED' or not p.get('verified_at_utc') for p in proofs):
        raise ValueError('INVALID_RETIRED_EMPTY_FOLDER_DISPOSITION')
    retired = {p['source_id'] for p in proofs}
    if len(retired) != len(proofs): raise ValueError('DUPLICATE_RETIRED_EMPTY_FOLDER')
    protected = {s['destination_id'] for s in sections.get('sections', [])}
    protected |= {d['id'] for s in sections.get('sections', [])
                  for d in s.get('created_destinations', []) + s.get('verified_destinations', [])}
    planned_paths = set()
    paths = {}; by_id = {n.get('id'):n for n in nodes if n.get('id')}
    for n in nodes:
        paths[n['i']] = (paths[n['p']] if n['p'] >= 0 else ()) + (n['n'].strip(),)
    if workbook is not None:
        rows = workbook.get('sheets', workbook.get('matrices', {}))['Proposed Renames']
        ix = {k:rows[0].index(k) for k in ('Source ID','Proposed name + type suffix','Proposed destination folder')}
        for row in rows[1:]:
            if len(row) <= max(ix.values()): continue
            n = by_id.get(row[ix['Source ID']])
            if not n or n.get('retired') or n.get('policy_excluded'): continue
            parent = path_parts(row[ix['Proposed destination folder']])
            if not parent: continue
            planned_paths.add(parent)
            if n['f'] and row[ix['Proposed name + type suffix']]:
                planned_paths.add(parent + (row[ix['Proposed name + type suffix']].strip(),))
    children = {}
    for n in nodes:
        children.setdefault(n['p'], []).append(n['i'])
    # Only discard hiding produced by the previous retirement pass. The input
    # viewer can also contain explicitly retained source wrappers and obsolete
    # proposal contexts; those are not evidence of a Drive trash operation.
    def clear_retirement(index):
        n = nodes[index]
        if not n.get('retained_source_container'):
            n.pop('viewer_hidden', None)
        for child in children.get(index, []):
            clear_retirement(child)
    for n in nodes:
        if n.pop('retired_empty_parent', None):
            clear_retirement(n['i'])
    if workbook is not None:
        retained_proposal_containers(data, workbook)
    for n in nodes:
        if n['f'] or n.get('retired') or n.get('policy_excluded'):
            continue
        parent = n['p']
        while parent >= 0:
            if nodes[parent].get('viewer_hidden'):
                raise ValueError('ACTIVE_PROPOSAL_UNDER_HIDDEN_FOLDER:' + n.get('id', ''))
            parent = nodes[parent]['p']
    def identity(n):
        return n.get('destination_overlay', {}).get('destination_id') or n.get('id') or n.get('context_id')
    def safe_branch(index):
        n = nodes[index]; sid = identity(n)
        if (n.get('pending_revision') or sid in protected or
                any(path[:len(paths[index])] == paths[index] for path in planned_paths)): return False
        if not n['f']: return bool(n.get('retired'))
        # Anonymous presentation contexts are not physical folder identities.
        if sid and sid not in retired: return False
        return all(safe_branch(child) for child in children.get(index, []))
    def hide(index):
        nodes[index]['viewer_hidden'] = True
        for child in children.get(index, []): hide(child)
    hidden = []
    for n in nodes:
        if n['f'] and identity(n) in retired and safe_branch(n['i']):
            n['retired_empty_parent'] = True; hide(n['i']); hidden.append(identity(n))
    data['retired_empty_folder_display'] = {'hidden_ids':sorted(set(hidden)),
        'retained_conflict_ids':sorted(retired - set(hidden)), 'basis':'verified_trash_history'}


CSS = '''\n/* UQMC destination execution overlay */
.migration-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:14px 0}
.migration-card{border:1px solid #c4d8ce;border-radius:10px;padding:14px;background:#f3faf6;color:#17362b}
.migration-card h3{margin:0 0 8px}.migration-card p{margin:6px 0;font-size:13px;line-height:1.5}
.migration-card a{color:#125d45;font-weight:650}.badge.migration-complete{background:#d9efe2;color:#185739}
.badge.migration-partial{background:#fff0ca;color:#755000}.badge.migration-pending{background:#edf0f3;color:#536171}
'''
JS_HELPER = '''
function folderMigrationBadge(n){
 const v=n.migration_summary;if(!v)return null;
 const text={COMPLETE:'Destination complete',PARTIAL:'Partially migrated',NOT_STARTED:'Not started · no tracked completion',SOURCE_RETAINED:'Source folder retained',NOT_TRACKED:'Source context · not tracked',REVISION_PENDING:'Proposed revision pending',DESTINATION_PENDING:'Container rename/move or verification pending',CURRENT_CONTAINER_RETAINED:'Existing empty folder retained'}[v.status];
 const cls=v.status==='COMPLETE'?'migration-complete':v.status==='PARTIAL'?'migration-partial':'migration-pending';
 const b=badge(text+(v.file_count&&['COMPLETE','PARTIAL'].includes(v.status)?' · '+v.completed_file_count+'/'+v.file_count+' files':''),cls);
 b.title='Verified destination-section completion at this snapshot. Not started means no tracked completion, not a claim about current live location.';
 return b;
}
function appendDestinationDetails(panel,n){
 const d=n.destination_overlay,v=n.migration_summary;if(!d&&!v)return;
 panel.append(el('h3','execution-detail-title','Destination migration'));
 const dl=el('dl','detail-grid');
 if(v)dl.append(el('dt','','Completion'),el('dd','',v.status==='COMPLETE'?'Verified complete':v.status==='PARTIAL'?'Partial':v.status==='REVISION_PENDING'?'Proposed layout pending; prior execution remains recorded':'No destination completion recorded'));
 if(n.pending_revision){dl.append(el('dt','','Proposed location'),el('dd','',n.pending_revision.target_path),el('dt','','Last verified layout'),el('dd','',n.pending_revision.before_path));}if(d){dl.append(el('dt','','Destination handling'),el('dd','',d.mode==='CREATED'?'New destination folder created; original source folders retained.':d.mode==='REUSED_EXISTING'?'Existing destination folder reused in place.':'Existing source folder moved to its approved destination.'));const a=el('a','source-link','Open actual destination in Drive ↗');a.href=safeLink(d.destination_url);a.target='_blank';a.rel='noopener noreferrer';panel.append(a);}
 if(v&&v.source_retained)dl.append(el('dt','','Original source folder'),el('dd','','Source folder retained; not moved or deleted by this run.'));
 panel.append(dl);
}
'''


HEADING_CSS = """
/* UQMC collapsed migration heading */
.execution-summary > details > summary{cursor:pointer;padding:8px 0}
.execution-summary > details > summary h2{display:inline}
.execution-summary > details > summary:focus-visible{outline:2px solid currentColor;outline-offset:4px}
"""


def heading_sections(data):
    """Hide redundant completed cards only; retain the full tree and history.

    Resolve aliases to nodes and walk actual parent links. A sibling sharing a
    text prefix is not an ancestor, and pending revisions cannot hide children.
    """
    entries = [(sec, resolve_folder_path(data, sec['path']))
               for sec in data['execution_sections']['sections']]
    def complete(sec):
        return sec['status'] == 'COMPLETED' and sec.get('proposal_status') != 'PENDING_REVISION'
    completed_roots = {root['i'] for sec, root in entries if complete(sec)}
    visible = []
    for sec, root in entries:
        parent = root['p']
        while parent >= 0 and parent not in completed_roots:
            parent = data['nodes'][parent]['p']
        if not complete(sec) or parent < 0:
            visible.append(sec)
    return visible


def render(source, data):
    packed = base64.b64encode(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)).decode()
    source = TREE.sub(lambda m: m.group(1) + packed + m.group(3), source, count=1)
    cards = []
    for sec in heading_sections(data):
        mode = ('Existing folder moved; its identity was preserved.' if sec['mode'] == 'REUSED_MOVED' else 'Existing destination folder reused in place.' if sec['mode'] == 'REUSED_EXISTING' else f"{sec['created_folder_count']} destination folders created; original source folders left in place.")
        retired_note = (f" {sec['retired_file_count']} retired (user-confirmed deleted; not migrated)."
                        if sec.get('retired_file_count') else '')
        excluded_note = (f" {sec['policy_excluded_file_count']} shortcuts excluded from migration; history retained."
                         if sec.get('policy_excluded_file_count') else '')
        pending_note = (f" Proposed revision pending for {sec['proposal_pending_file_count']} files; counts below describe the previous verified layout." if sec.get('proposal_status') == 'PENDING_REVISION' else '')
        cards.append('<article class="migration-card"><h3>' + html.escape(sec['title']) + '</h3><p><strong>' + html.escape('Revision pending' if pending_note else sec['status'].title()) + '</strong>' + html.escape(pending_note) + ' · ' + str(sec['completed_file_count']) + '/' + str(sec['expected_file_count']) + ' active files verified.' + html.escape(retired_note + excluded_note) + '</p><p>' + html.escape(mode) + '</p><a target="_blank" rel="noopener noreferrer" href="' + html.escape(folder_link(sec['destination_url']), quote=True) + '">Open actual destination ↗</a></article>')
    section = START + '<section class="execution-summary" aria-label="Migrated destination folders"><details class="migration-heading"><summary><h2>Migrated destination folders</h2></summary><p>Verified destination status from journal checkpoint ' + str(data['execution']['journal_sequence']) + '. Other folders show partial or not-started status in the tree. This is a static snapshot.</p><div class="migration-cards">' + ''.join(cards) + '</div></details></section>' + END
    if START in source:
        source = re.sub(re.escape(START) + '.*?' + re.escape(END), lambda _: section, source, flags=re.S)
    else:
        source = source.replace('<section class="execution-summary"', section + '<section class="execution-summary"', 1)
    if '/* UQMC collapsed migration heading */' not in source:
        source = source.replace('</style>', HEADING_CSS + '</style>', 1)
    if '/* UQMC destination execution overlay */' not in source:
        source = source.replace('</style>', CSS + '</style>', 1)
    if 'function folderMigrationBadge(' in source:
        source = re.sub(r'function folderMigrationBadge\(n\)\{.*?(?=function executionBadges\(n\)\{)', lambda _: JS_HELPER + '\n', source, count=1, flags=re.S)
    if 'function folderMigrationBadge(' not in source:
        source = source.replace('function executionBadges(n){', JS_HELPER + '\nfunction executionBadges(n){', 1)
        source = source.replace("const wrap=el('div','execution-badges');if(!n.id)return wrap;", "const wrap=el('div','execution-badges');if(n.f){const b=folderMigrationBadge(n);if(b)wrap.append(b);}if(n.destination_overlay)wrap.append(badge(n.destination_overlay.mode==='CREATED'?'New destination · source retained':n.destination_overlay.mode==='REUSED_EXISTING'?'Existing destination reused':'Existing folder moved','execution applied'));if(!n.id)return wrap;", 1)
        source = source.replace('if(n.id)nameGroup.append(executionBadges(n));', 'if(n.id||n.f)nameGroup.append(executionBadges(n));', 1)
        source = source.replace('panel.append(dl);appendExecutionDetails(panel,n);', 'panel.append(dl);appendDestinationDetails(panel,n);appendExecutionDetails(panel,n);', 1)
        source = source.replace("sourceUrl=safeLink(n.u);", "sourceUrl=safeLink(n.destination_overlay?n.destination_overlay.destination_url:n.u);", 1)
        source = source.replace("const label=map?'Open source map':'Open in Drive';", "const label=n.destination_overlay?'Open actual destination':map?'Open source map':'Open in Drive';", 1)
        source = source.replace("map?'Map':'Drive'", "n.destination_overlay?'Destination':map?'Map':'Drive'", 1)
        source = source.replace("if(n.id||n.context_id){const details", "if(n.id||n.context_id||n.destination_overlay){const details", 1)
        source = source.replace("n.holding?'Pending review · no destination assigned':n.context_id?", "n.destination_overlay?(n.destination_overlay.mode==='CREATED'?'Destination created and verified':'Destination verified'):n.holding?'Pending review · no destination assigned':n.context_id?", 1)
    revision_badge = "if(n.pending_revision){const r=el('div','execution-badges');r.append(badge('Revision pending · '+n.pending_revision.target_path,'migration-pending'));return r;}"
    if revision_badge not in source:
        source = source.replace('function executionBadges(n){', 'function executionBadges(n){' + revision_badge, 1)
    retirement_badge = "if(n.retired){const r=el('div','execution-badges');r.append(badge('Retired · user-confirmed deleted · not migrated','migration-pending'));return r;}"
    if retirement_badge not in source:
        source = source.replace('function executionBadges(n){', 'function executionBadges(n){' + retirement_badge, 1)
    exclusion_badge = "if(n.policy_excluded){const r=el('div','execution-badges');r.append(badge('Excluded shortcut · historical effects retained','migration-pending'));return r;}"
    if exclusion_badge not in source:
        source = source.replace('function executionBadges(n){', 'function executionBadges(n){' + exclusion_badge, 1)
    source = source.replace("const nodePair=n=>({folders:n.f?1:0,files:n.f?0:1,contexts:n.f&&!n.id?1:0});",
        "const nodePair=n=>n.viewer_hidden||n.policy_excluded?zeroPair():({folders:n.f?1:0,files:n.f?0:1,contexts:n.f&&!n.id?1:0});", 1)
    source = source.replace('match=N.map(n=>{const found=', 'match=N.map(n=>{if(n.viewer_hidden)return false;const found=', 1)
    source = source.replace('const n=N[i];if(active&&!visible[i])return;', 'const n=N[i];if(n.viewer_hidden)return;if(active&&!visible[i])return;', 1)
    source = source.replace('const children=n.c.filter(c=>!active||visible[c])', 'const children=n.c.filter(c=>!N[c].viewer_hidden&&(!active||visible[c]))', 1)
    source = source.replace('Drive links open originals; IDs stay in Details.', 'Destination links open verified migrated folders; source links and IDs remain in Details.')
    source = source.replace('The proposed reorganisation has not been executed. This viewer makes no changes. Drive links open the source item, even when a new name is proposed.', 'Completed destination sections and verified execution progress are shown below. This viewer makes no changes. Destination links open verified folders; source links remain in Details.')
    source = source.replace('Synthetic grouping folders have no execution status.', 'Grouping folders show destination completion separately from source actions.')
    source = source.replace('>Proposal only<', '>Proposal + execution snapshot<')
    source = re.sub(r'(?:Version \d+ · Updated[^<]+|Execution update · Journal checkpoint \d+)',
                    'Execution update · Journal checkpoint ' + str(data['execution']['journal_sequence']), source, count=1)
    return source


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--bundle', required=True, type=Path)
    p.add_argument('--workbook', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--report', required=True, type=Path)
    a = p.parse_args()
    source = a.source.read_text()
    data = unpack(source)
    original_ids = {n['id'] for n in data['nodes'] if n.get('id')}
    workbook = json.loads(a.workbook.read_text())
    workbook.setdefault('exported_at_utc', datetime.fromtimestamp(a.workbook.stat().st_mtime, timezone.utc).isoformat().replace('+00:00', 'Z'))
    report = refresh_proposal(data, workbook)
    bundle = json.loads(a.bundle.read_text())
    from uqmc_recovery.projection import validate_snapshot
    validate_snapshot(bundle['html'])
    integrate(data, bundle)
    if {n['id'] for n in data['nodes'] if n.get('id')} != original_ids:
        raise ValueError('SOURCE_IDENTITIES_CHANGED')
    result = render(source, data)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(result)
    report.update(snapshot_id=data['execution']['snapshot_id'], journal_sequence=data['execution']['journal_sequence'], source_identities=len(original_ids), sections=[{k:s[k] for k in ['section_id','status','expected_file_count','completed_file_count','mode']} for s in data['execution_sections']['sections']], output_sha256=hashlib.sha256(result.encode()).hexdigest(), output_bytes=len(result.encode()))
    a.report.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
