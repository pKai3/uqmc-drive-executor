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


def refresh_proposal(data, workbook):
    """Reconcile explicit rename rows, recognizing already-renamed folder aliases.

    Stable source IDs and source links never change. Unlisted/withheld identities
    are not introduced. The existing file tree is retained for records without
    a proposed rename row; nothing is inferred from absent spreadsheet rows.
    """
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
            candidates = [n for n in nodes if n['p'] == parent and n['s'] == 'destination' and n['f'] and part in {n['n'].strip(), (n.get('o') or '').strip()}]
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
            if n['n'] != target_name:
                raise ValueError('FOLDER_PROPOSAL_CHANGED_REQUIRES_REBUILD')
            continue
        parent = n['p']
        if n['s'] == 'destination':
            parent = resolve(path_parts(row[ix['Proposed destination folder']]))
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
        'source_folder_status_basis','verified_at_utc','verified_file_ids','verified_destinations'}
    destination_fields = {'id','name','parent_ids','verified_at_utc','destination_link','path','mode'}
    sections = {k: copy.deepcopy(v) for k,v in bundle['sections'].items()
                if k in {'schema_version','snapshot_id','generated_at_utc','journal_sequence',
                         'journal_tip_sha256','basis','other_sections'}}
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
    completed_files = set()
    retained = set()
    for section in sections['sections']:
        path = path_parts(section['path'])
        matches = path_nodes.get(path, [])
        if len(matches) != 1:
            raise ValueError('SECTION_PATH_NOT_UNIQUE:' + section['path'])
        root = matches[0]
        root_url = folder_link(section['destination_url'])
        members = [n for n in nodes if n['s'] == 'destination' and not n['f'] and paths[n['i']][:len(path)] == path]
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
            matches = path_nodes.get(dp, [])
            if len(matches) != 1:
                raise ValueError('CREATED_DESTINATION_PATH_NOT_UNIQUE:' + '/'.join(dp))
            d = destinations[fid]
            matches[0]['destination_overlay'] = {'destination_id': fid, 'destination_url': folder_link(d['destination_link']), 'mode': d.get('mode','CREATED'), 'section_id': section['section_id']}

    counts = {n['i']: [int(not n['f']), int(not n['f'] and n.get('id') in completed_files)] for n in nodes}
    for n in reversed(nodes):
        if n['p'] >= 0:
            for k in (0, 1):
                counts[n['p']][k] += counts[n['i']][k]
    for n in nodes:
        if not n['f']:
            continue
        total, done = counts[n['i']]
        if n['s'] == 'retained':
            state = 'SOURCE_RETAINED' if n.get('id') in retained else 'NOT_TRACKED'
        elif done and done == total or (not total and n.get('destination_overlay')):
            state = 'COMPLETE'
        elif done:
            state = 'PARTIAL'
        else:
            state = 'NOT_STARTED'
        n['migration_summary'] = {'status': state, 'file_count': total, 'completed_file_count': done, 'source_retained': n.get('id') in retained}
    data['execution'] = public
    data['execution_sections'] = sections
    data['notes'] = [re.sub(r'This remains a proposal; no source moves or renames have been executed\.', 'The tree combines the current proposal with the separately verified execution snapshot below; untracked records remain proposals.', x) for x in data['notes']]


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
 const text={COMPLETE:'Destination complete',PARTIAL:'Partially migrated',NOT_STARTED:'Not started · no tracked completion',SOURCE_RETAINED:'Source folder retained',NOT_TRACKED:'Source context · not tracked'}[v.status];
 const cls=v.status==='COMPLETE'?'migration-complete':v.status==='PARTIAL'?'migration-partial':'migration-pending';
 const b=badge(text+(v.file_count&&['COMPLETE','PARTIAL'].includes(v.status)?' · '+v.completed_file_count+'/'+v.file_count+' files':''),cls);
 b.title='Verified destination-section completion at this snapshot. Not started means no tracked completion, not a claim about current live location.';
 return b;
}
function appendDestinationDetails(panel,n){
 const d=n.destination_overlay,v=n.migration_summary;if(!d&&!v)return;
 panel.append(el('h3','execution-detail-title','Destination migration'));
 const dl=el('dl','detail-grid');
 if(v)dl.append(el('dt','','Completion'),el('dd','',v.status==='COMPLETE'?'Verified complete':v.status==='PARTIAL'?'Partial':'No destination completion recorded'));
 if(d){dl.append(el('dt','','Destination handling'),el('dd','',d.mode==='CREATED'?'New destination folder created; original source folders retained.':d.mode==='REUSED_EXISTING'?'Existing destination folder reused in place.':'Existing source folder moved to its approved destination.'));const a=el('a','source-link','Open actual destination in Drive ↗');a.href=safeLink(d.destination_url);a.target='_blank';a.rel='noopener noreferrer';panel.append(a);}
 if(v&&v.source_retained)dl.append(el('dt','','Original source folder'),el('dd','','Source folder retained; not moved or deleted by this run.'));
 panel.append(dl);
}
'''


def render(source, data):
    packed = base64.b64encode(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)).decode()
    source = TREE.sub(lambda m: m.group(1) + packed + m.group(3), source, count=1)
    cards = []
    for sec in data['execution_sections']['sections']:
        mode = ('Existing folder moved; its identity was preserved.' if sec['mode'] == 'REUSED_MOVED' else 'Existing destination folder reused in place.' if sec['mode'] == 'REUSED_EXISTING' else f"{sec['created_folder_count']} destination folders created; original source folders left in place.")
        cards.append('<article class="migration-card"><h3>' + html.escape(sec['title']) + '</h3><p><strong>' + html.escape(sec['status'].title()) + '</strong> · ' + str(sec['completed_file_count']) + '/' + str(sec['expected_file_count']) + ' files verified</p><p>' + html.escape(mode) + '</p><a target="_blank" rel="noopener noreferrer" href="' + html.escape(folder_link(sec['destination_url']), quote=True) + '">Open actual destination ↗</a></article>')
    section = START + '<section class="execution-summary" aria-label="Migrated destination folders"><h2>Migrated destination folders</h2><p>Verified destination status from journal checkpoint ' + str(data['execution']['journal_sequence']) + '. Other folders show partial or not-started status in the tree. This is a static snapshot.</p><div class="migration-cards">' + ''.join(cards) + '</div></section>' + END
    if START in source:
        source = re.sub(re.escape(START) + '.*?' + re.escape(END), lambda _: section, source, flags=re.S)
    else:
        source = source.replace('<section class="execution-summary"', section + '<section class="execution-summary"', 1)
    if '/* UQMC destination execution overlay */' not in source:
        source = source.replace('</style>', CSS + '</style>', 1)
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
    source = source.replace('Drive links open originals; IDs stay in Details.', 'Destination links open verified migrated folders; source links and IDs remain in Details.')
    source = source.replace('The proposed reorganisation has not been executed. This viewer makes no changes. Drive links open the source item, even when a new name is proposed.', 'Completed destination sections and verified execution progress are shown below. This viewer makes no changes. Destination links open verified folders; source links remain in Details.')
    source = source.replace('Synthetic grouping folders have no execution status.', 'Grouping folders show destination completion separately from source actions.')
    source = source.replace('>Proposal only<', '>Proposal + execution snapshot<')
    source = re.sub(r'Version \d+ · Updated[^<]+', 'Execution update · Journal checkpoint ' + str(data['execution']['journal_sequence']), source, count=1)
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
