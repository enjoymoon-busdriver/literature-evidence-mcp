from pathlib import Path
import subprocess
import unittest
from tests.node_runtime import NODE

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/literature_evidence_mcp/static'
HARNESS = ROOT / 'tests/formal_ui_dom.cjs'


def run_scenario(case: unittest.TestCase, scenario: str) -> None:
    completed = subprocess.run([str(NODE), str(HARNESS), str(STATIC), scenario],
                               text=True, capture_output=True, timeout=15)
    case.assertEqual(completed.returncode, 0, completed.stderr)


SELECTION = r'''
state.libraries = [{library_id:'lib-a', name:'A'}, {library_id:'lib-b', name:'B'}];
state.libraryId = 'lib-a'; state.snapshotId = 's1'; state.currentSnapshotId = 's1';
state.snapshots = [
  {snapshot_id:'s1', verified:true, members:[{document_id:'d1', title:'First'}]},
  {snapshot_id:'s2', verified:true, members:[{document_id:'d2', title:'Second'}]},
];
state.documents = state.snapshots[0].members;
'''


class FormalUiFrontendTests(unittest.TestCase):
    def test_late_document_and_section_cannot_cross_snapshot_or_library(self):
        run_scenario(self, SELECTION + r'''
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path,resolve}));
const oldDetail = loadDocumentDetail('d1');
state.snapshotId = 's2'; clearContentSelection(); state.documents = state.snapshots[1].members;
const newDetail = loadDocumentDetail('d2');
pending[1].resolve(response({library_id:'lib-a',snapshot_id:'s2',document:{document_id:'d2',title:'New'},toc:{items:[]}}));
await newDetail;
pending[0].resolve(response({library_id:'lib-a',snapshot_id:'s1',document:{document_id:'d1',title:'Old'},toc:{items:[]}}));
await oldDetail;
assert.equal(state.documentDetail.document.title, 'New');
const section = loadSection('sec-old');
state.libraryId = 'lib-b'; state.libraryEpoch++; clearContentSelection();
pending[2].resolve(response({library_id:'lib-a',snapshot_id:'s2',document_id:'d2',results:[{excerpt:'OLD'}]}));
await section;
assert.equal(state.sectionDetail, null);
assert.equal(state.documentDetail, null);
''')

    def test_late_search_cannot_replace_new_library_results_and_default_is_local(self):
        run_scenario(self, SELECTION + r'''
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path,options,resolve}));
const first = runSearch('battery');
assert.equal(JSON.parse(pending[0].options.body).mode, 'bm25');
assert.equal(JSON.parse(pending[0].options.body).snapshot_id, 's1');
assert(pending[0].path.includes('/lib-a/'));
state.libraryId = 'lib-b'; state.libraryEpoch++; clearContentSelection(); state.snapshotId = 's3';
state.snapshots = [{snapshot_id:'s3',verified:true,members:[]}];
const second = runSearch('other');
pending[1].resolve(response({library_id:'lib-b',snapshot_id:'s3',found:false,results:[]})); await second;
pending[0].resolve(response({library_id:'lib-a',snapshot_id:'s1',results:[{document_id:'d1',excerpt:'OLD'}]})); await first;
assert.deepEqual(state.search.results, []); assert.equal(state.search.query, 'other');
assert.equal(state.search.busy, false);
''')

    def test_browsing_and_activation_are_separate(self):
        run_scenario(self, SELECTION + r'''
route = (path, options) => {
  if (path.endsWith('/activate')) return Promise.resolve(response({library_id:'lib-a',current_snapshot_id:'s2'}));
  return new Promise(() => {});
};
browseSnapshot('s2', {quiet:true});
assert.equal(state.snapshotId, 's2'); assert.equal(state.currentSnapshotId, 's1');
assert(requests.every(r => !r.path.endsWith('/activate')));
browseSnapshot('s1', {quiet:true});
await activateSnapshot('s2');
assert.equal(state.snapshotId, 's1'); assert.equal(state.currentSnapshotId, 's2');
assert.equal(requests.filter(r => r.path.endsWith('/activate')).length, 1);
''')

    def test_untrusted_document_and_library_text_is_escaped(self):
        run_scenario(self, SELECTION + r'''
const attack = '<img src=x onerror="alert(1)">';
state.libraries[0].name = attack;
state.documents[0].title = attack; state.documents[0].source_name = attack;
const output = documentsPage();
assert(!output.includes(attack)); assert(output.includes('&lt;img'));
state.search.ran = true; state.search.results = [{document_id:'d1',title:attack,excerpt:attack,anchor_label:attack}];
assert(!searchPage().includes(attack));
libraryModal('lib-a');
assert(!nodes.get('modal-content').innerHTML.includes(attack));
assert(nodes.get('modal-content').innerHTML.includes('&lt;img'));
''')

    def test_failed_search_is_not_rendered_as_true_empty(self):
        run_scenario(self, SELECTION + r'''
route = () => Promise.resolve(response({error:'Synthetic provider failure',provider_audit:{call_count:2,calls:[]}}, false));
await runSearch('battery');
assert.equal(state.search.busy, false);
assert(state.search.error.includes('Synthetic provider failure'));
assert(searchPage().includes('搜索未完成'));
assert(!searchPage().includes('没有找到相关证据'));
assert(searchPage().includes('2'));
''')

    def test_late_document_list_does_not_restore_old_library(self):
        run_scenario(self, SELECTION + r'''
let release;
route = () => new Promise(resolve => {release = resolve;});
const pending = loadDocuments();
state.libraryId = 'lib-b'; state.libraryEpoch++; clearContentSelection(); state.snapshotId = '';
release(response({library_id:'lib-a',snapshot_id:'s1',documents:[{document_id:'d1',title:'Old'}]}));
await pending;
assert.deepEqual(state.documents, []); assert.equal(state.selectedDocumentId, '');
''')

    def test_dom_ready_loads_real_state_without_implicit_writes(self):
        run_scenario(self, r'''
route = path => {
  if (path === '/api/status') return Promise.resolve(response({csrf_token:'synthetic',connections:null,library_count:0}));
  if (path === '/api/libraries') return Promise.resolve(response({libraries:[]}));
  throw new Error('unexpected startup request: ' + path);
};
for (const ready of events.get('DOMContentLoaded') || []) await ready();
assert.deepEqual(requests.map(item => item.path), ['/api/status','/api/libraries']);
assert(requests.every(item => !item.options.method || item.options.method === 'GET'));
assert.equal(state.serviceReady, true); assert.equal(state.csrfToken, 'synthetic');
assert.equal(state.libraryId, ''); assert.equal(FolioConnections.enhancedEnabled(), false);
''')
