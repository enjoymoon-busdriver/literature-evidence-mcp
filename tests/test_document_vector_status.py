import unittest
from unittest import mock
from literature_evidence_mcp.vectors import build_vectors
from literature_evidence_mcp.errors import VectorError
from literature_evidence_mcp.library import FixedLibrary
from tests import test_formal_ui_followups as fixtures
from tests.test_formal_ui_backend import FakeEmbedder
from tests.test_formal_ui_frontend import run_scenario


class DocumentVectorStatusTests(unittest.TestCase):
    def setUp(self):
        fixtures.FollowupTests.setUp(self)

    def documents(self, snapshot=None):
        response = self.client.get(f'/api/libraries/{self.library_id}/snapshots/{snapshot or self.base}/documents')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['documents']

    def prepare(self, snapshot):
        config = self.runtime._config()
        return build_vectors(self.runtime.registry.library_path(self.library_id), snapshot,
                             config.vector_profile, FakeEmbedder(lambda:'SYNTHETIC', config, []))

    def test_exact_per_document_reuse_partial_configuration_and_unknown(self):
        self.assertEqual({d['vector_status'] for d in self.documents()}, {'missing'})
        subset = self.library.build([self.sources[0]])['snapshot_id']
        self.prepare(subset)
        from literature_evidence_mcp import vectors
        with mock.patch.object(vectors, '_verify_vector_state', wraps=vectors._verify_vector_state) as verify:
            states = self.documents()
            self.assertEqual(verify.call_count, 1)
        by_source = {d['source_name']:d for d in states}
        self.assertEqual(by_source['0.md']['vector_status'],'ready')
        self.assertEqual(by_source['1.md']['vector_status'],'missing')
        self.assertFalse(self.runtime.settings()['models_enabled'])
        self.sources[0].write_text(self.sources[0].read_text()+'\n# Additional\n\nA new uncovered passage.\n')
        changed = self.library.build([self.sources[0]])['snapshot_id']
        partial = self.documents(changed)[0]
        self.assertEqual(partial['vector_status'],'missing')
        self.assertGreater(partial['vector_covered_chunks'],0)
        self.assertLess(partial['vector_covered_chunks'],partial['vector_total_chunks'])
        self.assertEqual(self.documents(subset)[0]['vector_status'],'ready')
        self.prepare(self.base)
        self.assertEqual({d['vector_status'] for d in self.documents()}, {'ready'})
        ids=self.runtime.settings()['model_ids']; ids['vector_recall']='synthetic-new-profile'
        self.runtime.save_models(False,ids)
        self.assertEqual({d['vector_status'] for d in self.documents()}, {'mismatch'})
        self.runtime.restore_recommended_models()
        self.assertEqual({d['vector_status'] for d in self.documents()}, {'ready'})
        with mock.patch.object(vectors, '_verify_vector_state', side_effect=VectorError('synthetic corruption')):
            self.assertEqual({d['vector_status'] for d in self.documents()}, {'unknown'})
        other=self.runtime.registry.create('isolated-synthetic')
        other_root=self.runtime.registry.library_path(other['library_id'])
        other_snapshot=FixedLibrary(other_root,library_id=other['library_id']).build([self.sources[1]])['snapshot_id']
        self.assertEqual(vectors.document_vector_readiness(other_root,other_snapshot,self.runtime._config().vector_profile).popitem()[1]['vector_status'],'missing')
        self.credentials.get.assert_not_called()

    def test_frontend_badges_counts_and_same_snapshot_refresh(self):
        run_scenario(self,r'''
for (const [status,label] of [['ready','已向量化'],['missing','未向量化'],['mismatch','需重新准备'],['unknown','未确认']]) {
 assert(documentVectorBadge({vector_status:status}).includes(label));
}
assert(documentSubtitle({chunk_count:2}).includes('文本片段：2 段'));
assert(documentSubtitle({assets:[{chunk_count:1},{chunk_count:2}]}).includes('文本片段：3 段'));
state.libraryId='a'; state.snapshotId='s'; state.snapshots=[{snapshot_id:'s',verified:true,members:[]}];
route=path=>path.endsWith('/documents') ? response({library_id:'a',snapshot_id:'s',documents:[]}) : undefined;
browseSnapshot('s',{quiet:true}); await drain();
assert(requests.some(r=>r.path==='/api/libraries/a/snapshots/s/documents'));
''')
