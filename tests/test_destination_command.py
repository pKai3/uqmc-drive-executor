"""Complete one-command pipeline with fake remote Drive and real durable importer/runner."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_desired import FakeDrive, ProcessDeath
from test_importer import append_row
from uqmc_recovery.destination import run_destination
from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest

SECTION='Club/Skills'


def raw(sid,name,parents,folder=False):
    return {'id':sid,'name':name,'parents':parents,'mimeType':FOLDER if folder else 'application/pdf',
            'version':'1','modifiedTime':'2026-01-01T00:00:00Z','trashed':False,
            'owners':[{'permissionId':'fixture-owner'}],
            'capabilities':{'canRename':True,'canAddChildren':True,'canMoveItemWithinDrive':True,
                            'canRemoveChildren':True},
            **({} if folder else {'size':'10','md5Checksum':'fixture-content'})}


class Capture:
    def __init__(self, export):self.export=export;self.calls=0
    def _revision(self):
        return {'id':WORKBOOK_ID,'mimeType':'application/vnd.google-apps.spreadsheet',
                'trashed':False,'version':'1','modifiedTime':'2026-01-01T00:00:00Z'}
    def capture(self):
        self.calls+=1
        value=copy.deepcopy(self.export)
        value['exported_at_utc']=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        value.update(capture_method='sheets.spreadsheets.get_with_drive_revision_bracket',
                     source_revision=self._revision(),cell_evidence={})
        value['capture_sha256']=digest({key:value[key] for key in ('workbook_id','sheets','complete','cell_evidence','source_revision')})
        value['input_sha256']=catalog(value)['input_sha256']
        return value


class Server:
    def __init__(self,files):
        self.drive=FakeDrive(files);self.lock=threading.RLock();self.requests=[];self.created=[]
        self.counter=0;self.reads=[];self.after=None;self.before=None;self.journal=None;self.after_create=None
    def factory(self,allowed,mutations,excluded,enabled,**kwargs):
        return Client(self,set(allowed),set(mutations),set(excluded),enabled,kwargs.get('creation_parent_ids',set()))


class Client:
    audit_permissions=False
    def __init__(self,server,allowed,mutations,excluded,enabled,creation):
        self.server,self.allowed,self.mutations,self.excluded=server,allowed,mutations,excluded
        self.apply_enabled,self.creation=enabled,creation
    def get_account(self):return {'emailAddress':'fixture@example.test'}
    def check(self,sid):
        if sid not in self.allowed or sid in self.excluded:raise AssertionError('Out-of-scope read:'+sid)
    def get_file(self,sid):
        self.check(sid)
        with self.server.lock:
            self.server.reads.append(('get',sid));return self.server.drive.get_file(sid)
    def list_children(self,sid):
        self.check(sid)
        with self.server.lock:
            self.server.reads.append(('list',sid));return self.server.drive.list_children(sid)
    def list_permissions(self,*_):raise AssertionError('permissions.list must remain unused')
    def update_metadata(self,args,*,etag=None):
        sid=args['fileId'];self.check(sid)
        if not self.apply_enabled or sid not in self.mutations:raise AssertionError('Out-of-scope write:'+sid)
        with self.server.lock:
            if self.server.journal:
                events=[json.loads(line) for line in (Path(self.server.journal)/'events.jsonl').read_text().splitlines()]
                intent=next((e for e in reversed(events) if e['kind']=='DESIRED_ATTEMPTED' and e['payload']['source_id']==sid),None)
                if not intent or intent['payload']['args']!=args:raise AssertionError('No durable matching intent')
            self.server.requests.append(copy.deepcopy(args))
            if self.server.before:self.server.before(args)
            result=self.server.drive.update_metadata(args,etag=etag)
            if self.server.after:self.server.after(args,result)
            return result
    def reserve_folder_id(self,parent):
        if not self.apply_enabled or parent not in self.creation:raise AssertionError('Out-of-scope folder creation')
        with self.server.lock:
            self.server.counter+=1;return 'new-'+str(self.server.counter)
    def register_reserved_folder_id(self,sid,parent):self.allowed.add(sid)
    def get_reserved_folder(self,sid):
        with self.server.lock:
            if sid not in self.server.drive.files:raise TransportError('DRIVE_HTTP_ERROR',status=404)
            return copy.deepcopy(self.server.drive.files[sid])
    def create_reserved_folder(self,sid,*,name,parent_id,stable_key):
        if not self.apply_enabled or parent_id not in self.creation:raise AssertionError('Out-of-scope folder creation')
        with self.server.lock:
            value=raw(sid,name,[parent_id],True);value['appProperties']={'uqmcFolderKey':stable_key}
            value['_http_etag']='"fixture-v1"'
            self.server.drive.files[sid]=value;self.server.created.append(sid)
            if self.server.after_create:self.server.after_create(value)
            return copy.deepcopy(value)


class DestinationCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'workbook_id':WORKBOOK_ID,'token':'unused-fixture','expected_account':'fixture@example.test',
            'anchor_paths':{'Club':'club'},'accepted_inherited_access':True,'workers':2,
            'store':str(self.root/'state'),'run_directory':str(self.root/'runs'),
            'registry':str(self.root/'registry.json'),'backup_directory':str(self.root/'backups'),
            'viewer_file_id':'fixture-viewer'}
        self.export={'workbook_id':WORKBOOK_ID,'complete':{tab:True for tab in HEADERS},
                     'sheets':{tab:[list(headers)] for tab,headers in HEADERS.items()}}
        append_row(self.export,'Proposed Moves',{'Proposal ID':'MOVE-001','Action':'Move','Proposed target path':SECTION})
        append_row(self.export,'Move Manifest',{'Proposal ID':'MOVE-001','Manifest item':'ITEM-001',
                   'Source ID':'source-root','Exact target path':SECTION,'Scope':'Whole subtree including all descendants'})
        for ref,sid,old,new,parent in [('RENAME-001','source-root','Old folder','Skills','Club'),
                                     ('RENAME-002','leaf','Old.pdf','Approved.pdf',SECTION)]:
            append_row(self.export,'Proposed Renames',{'Rename ID':ref,'Source ID':sid,
                       'Current name + type suffix':old,'Proposed name + type suffix':new,'Proposed destination folder':parent})
        self.server=Server({'club':raw('club','Club',[],True),'old-parent':raw('old-parent','Old parent',[],True),
            'source-root':raw('source-root','Old folder',['old-parent'],True),
            'leaf':raw('leaf','Old.pdf',['source-root'])})
        self.server.journal=self.config['store'];self.capture=Capture(self.export)
        self.publications=[];self.progress=[]
    def publisher(self,registry):self.publications.append(copy.deepcopy(registry));return {'state':'VERIFIED'}
    def run_command(self,**kwargs):
        return run_destination(self.config,SECTION,capture_client=self.capture,factory=self.server.factory,
                               publisher=kwargs.pop('publisher',self.publisher),report=self.progress.append,**kwargs)
    def mixed(self):
        self.server.drive.files['eoi']=raw('eoi','Unrelated EOI.pdf',['source-root'])
    def test_preview_mixed_source_requires_only_destination_and_is_nonmutating(self):
        self.mixed();before=copy.deepcopy(self.server.drive.files)
        result=self.run_command()
        self.assertTrue(result['preview_only']);self.assertEqual(result['missing_folders'],[SECTION])
        self.assertEqual(result['retained_source_folder_ids'],['source-root'])
        self.assertEqual(self.server.drive.files,before);self.assertEqual(self.server.requests,[])
        self.assertEqual(Journal(self.config['store']).records(),[])
        self.assertNotIn(('get','eoi'),self.server.reads)
        self.assertEqual(self.capture.calls,1)
    def test_intact_folder_combined_apply_and_children_dependencies(self):
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.server.requests[0],{'fileId':'source-root','name':'Skills','addParents':'club','removeParents':'old-parent'})
        self.assertEqual(self.server.requests[1],{'fileId':'leaf','name':'Approved.pdf'})
        self.assertEqual(self.server.created,[]);self.assertEqual(self.server.drive.files['leaf']['parents'],['source-root'])
        self.assertEqual(result['publication']['state'],'VERIFIED')
        self.assertEqual((Path(result['backup'])/'events.jsonl').read_bytes(),Journal(self.config['store']).path.read_bytes())
        self.assertEqual(len(self.publications),1)
    def test_mixed_extraction_creates_destination_preserves_eoi_and_source_identity(self):
        self.mixed();before=copy.deepcopy(self.server.drive.files['source-root']);eoi=copy.deepcopy(self.server.drive.files['eoi'])
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':1},result)
        self.assertEqual(self.server.created,['new-1'])
        self.assertEqual(self.server.requests,[{'fileId':'leaf','name':'Approved.pdf','addParents':'new-1','removeParents':'source-root'}])
        self.assertEqual(self.server.drive.files['source-root'],before);self.assertEqual(self.server.drive.files['eoi'],eoi)
        entry=self.publications[0]['sections'][0]['publication_spec']
        self.assertEqual(entry['retained_source_folder_ids'],['source-root'])
    def test_completed_destination_apply_is_noop_from_fresh_sheet_and_history(self):
        self.run_command(apply=True);count=len(self.server.requests)
        result=self.run_command(apply=True)
        self.assertTrue(result['no_op'],result)
        self.assertEqual(result['counts'],{'COMPLETE':2})
        self.assertEqual(len(self.server.requests),count);self.assertEqual(self.capture.calls,2)
    def test_interruption_after_remote_success_resumes_without_replaying_folder(self):
        self.config['workers']=1
        def die(args,result):
            if args['fileId']=='source-root':raise ProcessDeath()
        self.server.after=die
        with self.assertRaises(ProcessDeath):self.run_command(apply=True)
        self.assertEqual(self.server.drive.files['source-root']['name'],'Skills')
        events=Journal(self.config['store']).records()
        self.assertTrue(any(e['kind']=='DESIRED_ATTEMPTED' for e in events))
        self.server.after=None
        result=self.run_command(apply=True,resume=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual([a['fileId'] for a in self.server.requests],['source-root','leaf'])
    def test_existing_destination_is_reused_without_moving_incoming_container(self):
        self.server.drive.files['existing']=raw('existing','Skills',['club'],True)
        before=copy.deepcopy(self.server.drive.files['source-root'])
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':1},result)
        self.assertEqual(self.server.requests,[{'fileId':'leaf','name':'Approved.pdf','addParents':'existing','removeParents':'source-root'}])
        self.assertEqual(self.server.drive.files['source-root'],before)
        self.assertEqual(self.server.created,[])

    def test_name_collision_stops_before_overwrite(self):
        self.mixed()
        self.server.drive.files['existing']=raw('existing','Skills',['club'],True)
        self.server.drive.files['occupant']=raw('occupant','Approved.pdf',['existing'])
        occupant=copy.deepcopy(self.server.drive.files['occupant'])
        result=self.run_command(apply=True)
        self.assertEqual(self.server.requests,[])
        self.assertEqual(self.server.drive.files['occupant'],occupant)
        self.assertTrue(result.get('blockers') or result['counts'].get('BLOCKED'),result)

    def test_lost_response_is_reconciled_once_then_finished(self):
        self.config['workers']=1
        lost=set()
        def lose(args,result):
            if args['fileId'] not in lost:
                lost.add(args['fileId'])
                raise TransportError('DRIVE_REQUEST_TIMEOUT',outcome='UNKNOWN')
        self.server.after=lose
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual([a['fileId'] for a in self.server.requests],['source-root','leaf'])

    def test_interruption_before_dispatch_resumes_from_live_original(self):
        self.config['workers']=1
        def die(args):raise ProcessDeath()
        self.server.before=die
        with self.assertRaises(ProcessDeath):self.run_command(apply=True)
        self.assertEqual(self.server.drive.files['source-root']['name'],'Old folder')
        self.server.before=None
        result=self.run_command(apply=True,resume=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.server.drive.files['source-root']['name'],'Skills')
        self.assertEqual([a['fileId'] for a in self.server.requests],['source-root','source-root','leaf'])

    def test_explicit_partial_rollback_restarts_without_repeating_completed_inverse(self):
        self.config['workers']=1
        result=self.run_command(apply=True);run_id=result['run_id']
        def die(args,value):
            if args.get('name')=='Old.pdf':raise ProcessDeath()
        self.server.after=die
        with self.assertRaises(ProcessDeath):self.run_command(apply=True,rollback=run_id)
        self.assertEqual(self.server.drive.files['leaf']['name'],'Old.pdf')
        self.assertEqual(self.server.drive.files['source-root']['name'],'Skills')
        self.server.after=None
        result=self.run_command(apply=True,rollback=run_id)
        self.assertEqual(result['counts'],{'ROLLED_BACK':2},result)
        self.assertEqual(self.server.drive.files['source-root']['name'],'Old folder')
        self.assertEqual(self.server.drive.files['source-root']['parents'],['old-parent'])
        inverse_leaf=[a for a in self.server.requests if a.get('name')=='Old.pdf']
        self.assertEqual(len(inverse_leaf),1)

    def test_create_remote_success_interruption_recovers_reserved_identity(self):
        self.mixed()
        def die(value):raise ProcessDeath()
        self.server.after_create=die
        with self.assertRaises(ProcessDeath):self.run_command(apply=True)
        self.assertEqual(self.server.created,['new-1'])
        self.assertEqual(self.server.requests,[])
        self.server.after_create=None
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':1},result)
        self.assertEqual(self.server.created,['new-1'])
        self.assertEqual(self.server.drive.files['leaf']['parents'],['new-1'])
        verified=[e for e in Journal(self.config['store']).records() if e['kind']=='REQUIRED_FOLDER_VERIFIED']
        self.assertEqual(len(verified),1)
        self.assertEqual(verified[0]['payload']['file_id'],'new-1')
        self.assertEqual(self.publications[-1]['sections'][0]['publication_spec']['destination_mode'],'CREATED')

    def test_publication_failure_preserves_verified_source_state_and_resume_never_replays(self):
        def unavailable(_):raise OSError('fixture publication unavailable')
        result=self.run_command(apply=True,publisher=unavailable)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(result['publication']['state'],'PENDING')
        before=list(self.server.requests)
        result=self.run_command(apply=True,resume=True)
        self.assertEqual(result['counts'],{'TARGET':2},result)
        self.assertEqual(self.server.requests,before)
        self.assertEqual(result['publication']['state'],'VERIFIED')


    def test_literal_date_slashes_in_original_binary_title_do_not_create_path_fragments(self):
        original='Certificate 01/02/2024.pdf'
        self.server.drive.files['leaf']['name']=original
        headers=HEADERS['Proposed Renames']
        self.export['sheets']['Proposed Renames'][2][headers.index('Current name + type suffix')]=original
        append_row(self.export,'Proposed Moves',{'Proposal ID':'MOVE-LEAF','Action':'Move',
            'Proposed target path':SECTION+'/'+original})
        append_row(self.export,'Move Manifest',{'Proposal ID':'MOVE-LEAF','Manifest item':'ITEM-LEAF',
            'Source ID':'leaf','Current item':original,'Exact target path':SECTION+'/'+original,'Scope':'Single item'})
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.server.created,[])
        self.assertEqual(self.server.drive.files['leaf']['name'],'Approved.pdf')
        self.assertEqual(self.server.drive.files['leaf']['parents'],['source-root'])
        self.assertEqual(self.server.requests,[
            {'fileId':'source-root','name':'Skills','addParents':'club','removeParents':'old-parent'},
            {'fileId':'leaf','name':'Approved.pdf'}])
        before=list(self.server.requests)
        preview=self.run_command()
        self.assertTrue(preview['preview_only'],preview)
        self.assertEqual(preview['counts'],{'COMPLETE':2},preview)
        self.assertEqual(preview['missing_folders'],[])
        self.assertEqual(self.server.requests,before)
        self.assertEqual(self.server.created,[])

if __name__=='__main__':unittest.main()
