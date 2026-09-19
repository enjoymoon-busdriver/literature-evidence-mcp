from pathlib import Path
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

from literature_evidence_mcp.catalog import snapshot_catalog_lock
from literature_evidence_mcp.registry import LibraryRegistryError, _RegistryWriteFailure
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.vectors import build_vectors
from tests import test_formal_ui_followups as fixtures
from tests.test_formal_ui_backend import FakeEmbedder, ORIGIN
from tests.test_formal_ui_frontend import run_scenario


class DeletionTests(unittest.TestCase):
    def setUp(self):
        fixtures.FollowupTests.setUp(self)
        self.name = 'DISPOSABLE_DELETE_PRIMARY'
        self.runtime.registry.rename(self.library_id, self.name)
        self.target = self.runtime.registry.library_path(self.library_id)
        self.other = self.runtime.registry.create('DISPOSABLE_DELETE_OTHER')
        self.other_path = self.runtime.registry.library_path(self.other['library_id'])

    def delete(self, name=None, library_id=None, headers=None):
        target_id = library_id or self.library_id
        # Read-only target inventory before every actual deletion attempt.
        records = self.runtime.registry.list_libraries()
        record = next(item for item in records if item['library_id'] == target_id)
        path = Path(record['library_root'])
        self.assertTrue(record['name'].startswith('DISPOSABLE_DELETE_'))
        self.assertEqual(path.parent.resolve(), (self.root / 'app' / 'libraries').resolve())
        self.assertEqual(path.name, target_id)
        self.assertFalse(path.is_symlink())
        return self.client.post(f'/api/libraries/{target_id}/delete',
            json={'confirmation_name': self.name if name is None else name},
            headers=headers if headers is not None else {'Origin': ORIGIN, 'X-CSRF-Token': self.csrf, 'X-Action-Intent':'delete-library'})

    def test_delete_files_vectors_discovery_and_last_library(self):
        config = self.runtime._config()
        build_vectors(self.target, self.base, config.vector_profile, FakeEmbedder(lambda:'SYNTHETIC',config,[]))
        self.assertTrue((self.target/'derived'/'vectors').is_dir())
        other_library = FixedLibrary(self.other_path, library_id=self.other['library_id'])
        other_snapshot = other_library.build([self.sources[0]])['snapshot_id']
        build_vectors(self.other_path, other_snapshot, config.vector_profile, FakeEmbedder(lambda:'SYNTHETIC',config,[]))
        other_files = {p.relative_to(self.other_path): p.read_bytes() for p in self.other_path.rglob('*') if p.is_file()}
        settings_before = self.runtime.settings()
        self.runtime.registry.select(self.library_id)
        response = self.delete()
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(self.target.exists()); self.assertTrue(self.other_path.exists())
        with self.assertRaises(Exception): self.library.build(self.sources)
        with self.assertRaises(Exception): FixedLibrary(self.target, library_id=self.library_id)
        self.assertFalse(self.target.exists())
        self.assertTrue(all(source.exists() for source in self.sources))
        self.assertEqual(self.runtime.settings(),settings_before)
        self.assertEqual(other_files, {p.relative_to(self.other_path): p.read_bytes() for p in self.other_path.rglob('*') if p.is_file()})
        self.assertEqual(response.json()['selected_library_id'],self.other['library_id'])
        self.assertGreaterEqual(self.client.get(f'/api/libraries/{self.library_id}/snapshots').status_code,400)
        from literature_evidence_mcp.mcp_tools import ReadOnlyEvidenceTools
        with self.assertRaises(Exception):
            ReadOnlyEvidenceTools(self.runtime.root).get_document_metadata(self.library_id,self.base,self.members[0]['document_id'])
        response = self.delete(self.other['name'],self.other['library_id'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(self.other_path.exists()); self.assertEqual(self.runtime.registry.list_libraries(),[])
        self.assertIsNone(response.json()['selected_library_id'])
        self.assertTrue(self.runtime.registry.create('DISPOSABLE_DELETE_NEW')['library_id'])
        self.credentials.get.assert_not_called()

    def test_name_id_csrf_busy_and_path_boundaries(self):
        for name in ('wrong', self.name+' '):
            self.assertGreaterEqual(self.delete(name).status_code,400)
        self.assertGreaterEqual(self.delete(self.other['name']).status_code,400)
        self.assertGreaterEqual(self.delete(headers={}).status_code,400)
        self.assertGreaterEqual(self.delete(headers={'Origin':'http://evil.invalid','X-CSRF-Token':self.csrf,'X-Action-Intent':'delete-library'}).status_code,400)
        self.assertGreaterEqual(self.delete(headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf,'X-Action-Intent':'remove-documents'}).status_code,400)
        with snapshot_catalog_lock(self.target):
            response=self.delete()
            self.assertGreaterEqual(response.status_code,400)
        for value in ('../', 'lib_'+'0'*32, str(self.root)):
            with self.assertRaises(LibraryRegistryError): self.runtime.registry.delete(value,self.name)
        self.assertTrue(self.target.exists()); self.assertTrue(self.other_path.exists())
        outside = self.root/'outside'; outside.mkdir(); (outside/'keep').write_text('keep')
        (self.target/'external-link').symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.delete().status_code,200)
        self.assertEqual((outside/'keep').read_text(),'keep')

    def test_active_vector_request_rejects_delete_until_finished(self):
        entered, release = threading.Event(), threading.Event()
        config = self.runtime._config()
        class SlowEmbedder(FakeEmbedder):
            def __call__(self, texts, profile):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test did not release vector request')
                return super().__call__(texts, profile)
        self.runtime._embedder_factory = lambda key, cfg: SlowEmbedder(lambda: 'SYNTHETIC', cfg, [])
        settings = self.runtime.settings(); settings.update(models_enabled=True, aliyun_configured=True)
        self.runtime._write(settings)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.post, f'/api/libraries/{self.library_id}/vectors/build',
                json={'snapshot_id':self.base}, headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf,'X-Action-Intent':'vectors-build'})
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.delete().status_code,409)
                self.assertTrue(self.target.exists())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).status_code,200)
        self.assertEqual(self.delete().status_code,200)
        self.assertFalse(self.target.exists())

    def test_symlink_library_is_rejected_without_deleting_external_files(self):
        moved=self.target.with_name('preserved-disposable-library')
        self.target.rename(moved)
        self.target.symlink_to(self.other_path,target_is_directory=True)
        with self.assertRaises(LibraryRegistryError):
            self.runtime.registry.delete(self.library_id,self.name)
        self.assertTrue(moved.exists()); self.assertTrue(self.other_path.exists())

    def test_partial_cleanup_unregisters_and_reports_failure(self):
        original=shutil.rmtree
        def fail(*args,**kwargs): raise PermissionError('synthetic cleanup failure')
        fail.avoids_symlink_attacks=original.avoids_symlink_attacks
        with mock.patch('literature_evidence_mcp.registry.shutil.rmtree',fail):
            response=self.delete()
        self.assertGreaterEqual(response.status_code,400)
        self.assertIn('清理未完成',response.json()['error'])
        self.assertTrue(self.target.exists()); self.assertTrue(self.other_path.exists())
        self.assertNotIn(self.library_id,[r['library_id'] for r in self.runtime.registry.list_libraries()])
        self.assertGreaterEqual(self.client.get(f'/api/libraries/{self.library_id}/snapshots').status_code,400)

    def test_registry_write_failure_keeps_files_and_registration(self):
        with mock.patch.object(self.runtime.registry.__class__,'_write',side_effect=_RegistryWriteFailure('synthetic',published=False)):
            self.assertGreaterEqual(self.delete().status_code,400)
        self.assertTrue(self.target.exists())
        self.assertIn(self.library_id,[r['library_id'] for r in self.runtime.registry.list_libraries()])

    def test_published_registry_write_failure_does_not_expose_old_id(self):
        original = self.runtime.registry.__class__._write
        def published(instance, descriptor, value):
            original(instance, descriptor, value)
            raise _RegistryWriteFailure('synthetic fsync failure', published=True)
        with mock.patch.object(self.runtime.registry.__class__, '_write', published):
            response = self.delete()
        self.assertGreaterEqual(response.status_code, 400)
        self.assertTrue(self.target.exists())
        self.assertNotIn(self.library_id, [r['library_id'] for r in self.runtime.registry.list_libraries()])
        self.assertIn('文件尚未清理', response.json()['error'])

    def test_ui_cancel_exact_confirmation_binding_and_empty_state(self):
        run_scenario(self,r'''
state.libraryId='a'; state.snapshotId='s'; state.currentSnapshotId='s';
state.libraries=[{library_id:'a',name:'DISPOSABLE_DELETE_SAME',selected:true},{library_id:'b',name:'DISPOSABLE_DELETE_SAME'}];
state.snapshots=[{snapshot_id:'s',verified:true,members:[]}];
route=(path,options)=> {
 if(path==='/api/libraries') return response({libraries:state.libraries});
 if(path==='/api/libraries/a/snapshots') return response({library_id:'a',current_snapshot_id:'s',snapshots:state.snapshots});
};
await deleteLibraryDialog('a');
assert(nodes.get('modal-content').innerHTML.includes('同名库请核对'));
assert(nodes.get('modal-content').innerHTML.includes('不可恢复'));
const before=requests.length;
for(const fn of events.get('click')) await fn({target:{closest:()=>({dataset:{action:'close-modal'},disabled:false})}});
assert.equal(requests.length,before);
nodes.get('delete-library-name').value='wrong'; await deleteLibrary(); assert.equal(requests.length,before);
state.libraryId='b'; nodes.get('delete-library-name').value='DISPOSABLE_DELETE_SAME';
route=(path,options)=> {
 if(path==='/api/libraries/a/delete') {assert.equal(JSON.parse(options.body).confirmation_name,'DISPOSABLE_DELETE_SAME'); return response({deleted:true,deleted_library_id:'a'});}
 if(path==='/api/libraries') return response({libraries:[]});
};
await deleteLibrary();
assert(requests.some(r=>r.path==='/api/libraries/a/delete'));
assert.equal(state.libraryId,''); assert.equal(state.snapshotId,''); assert.equal(state.currentSnapshotId,'');
assert.deepEqual(state.documents,[]); assert.deepEqual(state.removalIds,[]); assert.deepEqual(state.search.results,[]);
assert(documentsPage().includes('新建文献库'));
''')
