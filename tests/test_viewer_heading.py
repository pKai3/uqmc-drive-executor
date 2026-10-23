"""Display-only parent filtering and native accessible collapsed summary."""
import base64
import copy
import gzip
from html.parser import HTMLParser
import json
import unittest

from uqmc_recovery import viewer_publication as v


def fixture(paths):
    data={'nodes':[], 'execution':{'journal_sequence':13317}, 'execution_sections':{'sections':[]}}
    nodes={}
    for path,state in paths:
        parent=-1
        for name in path.split('/'):
            key=(parent,name)
            if key not in nodes:
                index=len(data['nodes']);nodes[key]=index
                data['nodes'].append({'i':index,'p':parent,'n':name,'s':'destination','f':True,'o':None})
            parent=nodes[key]
        data['execution_sections']['sections'].append(dict(title=path,path=path,status=state,
            mode='REUSED_EXISTING',completed_file_count=1,expected_file_count=1,
            destination_url='https://drive.google.com/drive/folders/fixture-'+str(parent)))
    return data


class HeaderParser(HTMLParser):
    def __init__(self):super().__init__();self.details=[];self.summaries=0
    def handle_starttag(self,tag,attrs):
        if tag=='details':self.details.append(dict(attrs))
        if tag=='summary':self.summaries+=1


class ViewerHeadingTests(unittest.TestCase):
    def titles(self,data):return [s['title'] for s in v.heading_sections(data)]

    def test_complete_parent_suppresses_child_regardless_of_order(self):
        data=fixture([('Root/Child','COMPLETED'),('Root','COMPLETED')])
        self.assertEqual(self.titles(data),['Root'])

    def test_partial_parent_keeps_completed_child(self):
        data=fixture([('Root','PARTIAL'),('Root/Child','COMPLETED')])
        self.assertEqual(self.titles(data),['Root','Root/Child'])

    def test_sibling_prefix_is_not_ancestry(self):
        data=fixture([('Root/Training','COMPLETED'),('Root/Training Materials','COMPLETED')])
        self.assertEqual(len(self.titles(data)),2)

    def test_nested_cards_keep_only_highest_completed_ancestor(self):
        data=fixture([('Root','PARTIAL'),('Root/A/B/C','COMPLETED'),('Root/A','COMPLETED'),
                      ('Root/A/B','COMPLETED'),('Root/D','COMPLETED')])
        self.assertEqual(self.titles(data),['Root','Root/A','Root/D'])

    def test_pending_parent_revision_keeps_completed_child(self):
        data=fixture([('Root','COMPLETED'),('Root/Child','COMPLETED')])
        data['execution_sections']['sections'][0]['proposal_status']='PENDING_REVISION'
        self.assertEqual(self.titles(data),['Root','Root/Child'])

    def test_pending_child_revision_remains_visible(self):
        data=fixture([('Root','COMPLETED'),('Root/Child','COMPLETED')])
        data['execution_sections']['sections'][1]['proposal_status']='PENDING_REVISION'
        self.assertEqual(self.titles(data),['Root','Root/Child'])

    def test_verified_alias_resolves_to_same_tree_ancestor(self):
        data=fixture([('Root','COMPLETED'),('Root/Old child','COMPLETED')])
        data['nodes'][1].update(n='New child',o='Old child')
        self.assertEqual(self.titles(data),['Root'])

    def test_render_is_collapsed_accessible_and_preserves_tree_counts_and_history(self):
        data=fixture([('Root','COMPLETED'),('Root/Child','COMPLETED')])
        before=copy.deepcopy(data)
        packed=base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        source='<html><style></style><script id="tree-data">'+packed+'</script><details id="tree-control" open><summary>Tree</summary></details><section class="execution-summary"></section></html>'
        once=v.render(source,data);twice=v.render(once,data)
        header=twice.split(v.START)[1].split(v.END)[0]
        parser=HeaderParser();parser.feed(header)
        self.assertEqual(parser.details,[{'class':'migration-heading'}])
        self.assertEqual(parser.summaries,1)
        self.assertEqual(header.count('class="migration-card"'),1)
        self.assertIn('<h3>Root</h3>',header);self.assertNotIn('<h3>Root/Child</h3>',header)
        self.assertIn('<details id="tree-control" open>',twice)
        self.assertEqual(v.unpack(twice),before)
        self.assertEqual(data,before)
        self.assertEqual(twice.count('/* UQMC collapsed migration heading */'),1)
        self.assertEqual(twice.count(v.START),1)


if __name__=='__main__':unittest.main()
