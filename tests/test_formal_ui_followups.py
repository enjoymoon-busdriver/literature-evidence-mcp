from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from starlette.testclient import TestClient
from literature_evidence_mcp.connections import Connections
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.vectors import build_vectors
from literature_evidence_mcp.web import create_app
from tests.test_formal_ui_backend import FakeEmbedder, ORIGIN, PORT
from tests.test_formal_ui_frontend import run_scenario

ROOT = Path(__file__).resolve().parents[1]


class FollowupTests(unittest.TestCase):
    def setUp(self):
        artifacts = ROOT / 'local-artifacts' / 'formal-ui'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts, prefix='followup-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = mock.Mock()
        self.tunnel = mock.Mock()
        self.tunnel.status.return_value = {'state': 'stopped', 'passed': True}
        self.runtime = Connections(self.root / 'app', credentials=self.credentials, tunnel=self.tunnel)
        record = self.runtime.registry.create('合成移除测试')
        self.library_id = record['library_id']
        self.library = FixedLibrary(self.runtime.registry.library_path(self.library_id), library_id=self.library_id)
        self.sources = []
        for i in range(3):
            source = self.root / f'{i}.md'
            source.write_text(f'# Synthetic {i}\n\nUnique evidence {i}.\n')
            self.sources.append(source)
        self.base = self.library.build(self.sources)['snapshot_id']
        self.members = self.library.snapshot_members(self.base)
        self.client = TestClient(create_app(self.runtime.root, port=PORT, connections=self.runtime), base_url=ORIGIN)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.csrf = self.client.get('/api/status').json()['csrf_token']

    def remove(self, ids, base=None, headers=None):
        return self.client.post(f'/api/libraries/{self.library_id}/remove-documents',
            json={'base_snapshot_id': base or self.base, 'remove_document_ids': ids},
            headers=headers if headers is not None else {'Origin': ORIGIN, 'X-CSRF-Token': self.csrf, 'X-Action-Intent': 'remove-documents'})

    def states(self):
        response = self.client.get(f'/api/libraries/{self.library_id}/snapshots')
        self.assertEqual(response.status_code, 200, response.text)
        return {v['snapshot_id']: v['vector_status'] for v in response.json()['snapshots']}

    def test_single_batch_removal_preserves_base_sources_pointer_and_vector_identity(self):
        profile = self.runtime._config().vector_profile
        calls = []
        build_vectors(self.runtime.registry.library_path(self.library_id), self.base, profile, FakeEmbedder(lambda: 'SYNTHETIC', self.runtime._config(), calls))
        for count in (1, 2):
            response = self.remove([m['document_id'] for m in self.members[:count]])
            self.assertEqual(response.status_code, 201, response.text)
            result = response.json()
            self.assertEqual(result['current_snapshot_id'], self.base)
            self.assertEqual(result['difference']['counts']['removed'], count)
            self.assertEqual(result['difference']['counts']['inherited'], 3-count)
            self.assertEqual(result['difference']['counts']['added'], 0)
            self.assertEqual(len(self.library.snapshot_members(result['snapshot_id'])), 3-count)
            self.assertEqual(self.states()[result['snapshot_id']], 'missing')
        self.assertEqual(len(self.library.snapshot_members(self.base)), 3)
        self.assertTrue(all(source.exists() for source in self.sources))
        self.assertEqual(self.states()[self.base], 'ready')
        self.credentials.get.assert_not_called()

    def test_invalid_cross_library_all_removal_and_csrf_leave_catalog_unchanged(self):
        before = self.library.list_snapshots()
        other = self.runtime.registry.create('other')
        foreign = FixedLibrary(self.runtime.registry.library_path(other['library_id']), library_id=other['library_id'])
        source = self.root / 'foreign.md'; source.write_text('# Foreign\n\nAnother source.\n')
        other_snapshot = foreign.build([source])['snapshot_id']
        foreign_id = foreign.snapshot_members(other_snapshot)[0]['document_id']
        for ids, base in [(['invalid'], None), ([foreign_id], None), ([self.members[0]['document_id']], other_snapshot), ([m['document_id'] for m in self.members], None), ([self.members[0]['document_id']]*2, None), ([], None)]:
            response = self.remove(ids, base)
            self.assertGreaterEqual(response.status_code, 400, response.text)
            self.assertEqual(self.library.list_snapshots(), before)
        for headers in ({}, {'Origin': 'http://evil.invalid', 'X-CSRF-Token': self.csrf, 'X-Action-Intent': 'remove-documents'}, {'Origin': ORIGIN, 'X-CSRF-Token': self.csrf, 'X-Action-Intent': 'build-snapshot'}):
            self.assertGreaterEqual(self.remove([self.members[0]['document_id']], headers=headers).status_code, 400)
        self.assertEqual(self.library.list_snapshots(), before)
        from literature_evidence_mcp.errors import ImportPolicyError
        with mock.patch.object(FixedLibrary, 'build', side_effect=ImportPolicyError('synthetic failure')):
            self.assertGreaterEqual(self.remove([self.members[0]['document_id']]).status_code, 400)
        self.assertEqual(self.library.list_snapshots(), before)

    def test_vector_exact_profile_reuse_disabled_changed_and_unknown(self):
        calls = []
        config = self.runtime._config()
        embedder = FakeEmbedder(lambda: 'SYNTHETIC', config, calls)
        self.assertEqual(self.states()[self.base], 'missing')
        build_vectors(self.runtime.registry.library_path(self.library_id), self.base, config.vector_profile, embedder)
        count = len(calls)
        result = build_vectors(self.runtime.registry.library_path(self.library_id), self.base, config.vector_profile, embedder)
        self.assertTrue(result['already_exists']); self.assertEqual(len(calls), count)
        self.assertEqual(self.states()[self.base], 'ready')
        ids = self.runtime.settings()['model_ids']; ids['vector_recall'] = 'synthetic-other-model'
        self.runtime.save_models(False, ids)
        self.assertEqual(self.states()[self.base], 'mismatch')
        self.runtime.restore_recommended_models()
        self.assertEqual(self.states()[self.base], 'ready')
        from literature_evidence_mcp.errors import VectorError
        with mock.patch('literature_evidence_mcp.vectors.vector_readiness', side_effect=VectorError('broken')):
            self.assertEqual(self.states()[self.base], 'unknown')
        self.credentials.get.assert_not_called()

    def test_vector_running_and_failure_are_observed_operations(self):
        settings = self.runtime.settings()
        settings.update(models_enabled=True, aliyun_configured=True)
        self.runtime._write(settings)
        from literature_evidence_mcp.errors import VectorError
        def fail(*args, **kwargs):
            self.assertEqual(self.runtime.vector_states(self.library_id)[self.base], 'preparing')
            raise VectorError('synthetic build failure')
        with mock.patch('literature_evidence_mcp.vectors.build_vectors', side_effect=fail):
            with self.assertRaises(VectorError):
                self.runtime.build_vectors(self.library_id, self.base)
        self.assertEqual(self.states()[self.base], 'failed')
        self.credentials.get.assert_not_called()

    def test_labels_timezone_collision_copy_and_pointer_marks(self):
        with mock.patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}):
            run_scenario(self, r'''
state.libraryId='a'; state.libraries=[{library_id:'a',name:'A'}];
const a = '20260919T070001000000Z-111111111111-aaaa1234';
const b = '20260919T070002000000Z-111111111111-bbbb1234';
const c = '20260919T070001000001Z-111111111111-cccc1234';
state.snapshots = [{snapshot_id:a,verified:true,members:[],vector_status:'ready'}];
assert.match(shortSnapshot(a), /9 月 19 日 15:00$/);
state.snapshots.push({snapshot_id:b,verified:true,members:[],vector_status:'mismatch'});
assert.match(shortSnapshot(a), /15:00:01$/); assert.match(shortSnapshot(b), /15:00:02$/);
state.snapshots.push({snapshot_id:c,verified:true,members:[],vector_status:'unknown'});
assert.notEqual(shortSnapshot(a), shortSnapshot(c));
state.snapshotId = b; state.currentSnapshotId = a; state.lastSuccessfulSnapshotId = c;
const html = versionsPage();
assert(html.includes('当前激活')); assert(html.includes('正在浏览')); assert(html.includes('上次成功'));
assert(html.includes('<details')); assert(html.includes('复制完整 ID')); assert(html.includes(a));
assert(html.includes('已就绪')); assert(html.includes('需要重新准备')); assert(html.includes('未确认'));
let copied; navigator.clipboard = {writeText: async text => {copied=text;}};
await copySnapshotId(a); assert.equal(copied,a);
''')

    def test_selection_filters_scope_cancel_and_failure(self):
        run_scenario(self, r'''
state.libraryId='a'; state.snapshotId='s1'; state.currentSnapshotId='s1';
state.libraries=[{library_id:'a',name:'A'}];
state.snapshots=[{snapshot_id:'s1',verified:true,members:[]},{snapshot_id:'s2',verified:true,members:[]}];
state.documents=[{document_id:'d1',title:'keep'},{document_id:'d2',title:'remove'},{document_id:'d3',title:'remove'}];
state.managing=true;
const click=async (action) => {for(const fn of events.get('click')) await fn({target:{closest:()=>({dataset:{action},disabled:false})}});};
state.filter='remove'; await click('select-filtered'); assert.deepEqual(state.removalIds,['d2','d3']);
assert(documentsPage().includes('data-removal-id="d2"')); assert(documentsPage().includes('从新版本中移除（2 篇）'));
removalModal(); assert(nodes.get('modal-content').innerHTML.includes('当前激活版本不会自动切换'));
const count=requests.length; await click('close-modal'); assert.equal(requests.length,count);
for(const fn of events.get('input')) fn({target:{id:'document-filter',value:'keep',selectionStart:4}});
assert.deepEqual(state.removalIds,[]);
state.filter=''; state.removalIds=['d1','d2','d3'];
assert(documentsPage().includes('不能移除全部文档或最后一篇'));
state.removalIds=['d1']; removalModal(); route=()=>response({error:'synthetic failure'},false);
await removeDocuments(); assert.equal(state.currentSnapshotId,'s1'); assert.deepEqual(state.removalIds,['d1']); assert.equal(state.building,false);
state.removalIds=['d1']; browseSnapshot('s2'); assert.deepEqual(state.removalIds,[]);
state.removalIds=['d1']; clearContentSelection(); assert.deepEqual(state.removalIds,[]);
''')
