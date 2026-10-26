"""Verified destination-ID aliases; never additional execution authority."""
from .model import FOLDER, SafetyError, digest
from .section_prepare import _path


def destination_mappings(rows):
    paths={};ids={};refs=set()
    for row in rows:
        path=_path(row.get('Destination folder path'))
        sid=row.get('Source folder ID');ref=row.get('Proposal ID')
        current=row.get('Current folder name');proposed=row.get('Proposed folder name')
        if not all(isinstance(v,str) and v for v in (sid,ref,current,proposed)):
            raise SafetyError('DESTINATION_MAPPING_FIELDS_REQUIRED:'+path)
        if proposed!=path.rsplit('/',1)[-1]:
            raise SafetyError('DESTINATION_MAPPING_BASENAME_DISAGREEMENT:'+path)
        if path in paths or sid in ids or ref in refs:
            raise SafetyError('DESTINATION_MAPPING_DUPLICATE_ID_PATH_OR_REFERENCE:'+path)
        blocked=(str(row.get('Decision (Yes/No)','')).strip().lower() in {'no','excluded','withdrawn'}
                 or str(row.get('Review state','')).strip().lower() in {'excluded','withdrawn'})
        paths[path]={'source_id':sid,'proposal_ref':ref,'logical_path':path,
            'current_name':current,'proposed_name':proposed,'blocked':blocked,
            'row_number':row['_row'],'row_sha256':digest(row)}
        ids[sid]=path;refs.add(ref)
    return paths


def verify_mapping(mapping,state,parent_id,*,children=None):
    path=mapping['logical_path'];sid=mapping['source_id']
    if mapping['blocked']:raise SafetyError('DESTINATION_MAPPING_WITHDRAWN:'+path)
    if (state.get('id')!=sid or state.get('mime_type')!=FOLDER
            or state.get('trashed') is not False):
        raise SafetyError('DESTINATION_MAPPING_NOT_ACTIVE_FOLDER:'+path)
    if state.get('name') not in {mapping['current_name'],mapping['current_name'].rstrip(),mapping['proposed_name']}:
        raise SafetyError('DESTINATION_MAPPING_NAME_DRIFT:'+path+':'+sid)
    if parent_id is None or state.get('parents')!=[parent_id]:
        raise SafetyError('DESTINATION_MAPPING_PARENT_DRIFT:'+path+':'+sid)
    if children is None:return
    own=[c for c in children if c.get('id')==sid]
    if (len(own)!=1 or own[0].get('name')!=state['name']
            or own[0].get('mimeType',own[0].get('mime_type'))!=FOLDER):
        raise SafetyError('DESTINATION_MAPPING_PARENT_MEMBERSHIP_CONFLICT:'+path+':'+sid)
    names={mapping['current_name'].rstrip(),mapping['proposed_name'].rstrip()}
    collisions=[c['id'] for c in children if c.get('id')!=sid and c.get('name','').rstrip() in names]
    if collisions:
        raise SafetyError('DESTINATION_MAPPING_NAME_COLLISION:'+path+':'+','.join(sorted(collisions)))
