import unittest
from tests.test_formal_ui_frontend import run_scenario


class DocumentSelectionHighlightTests(unittest.TestCase):
    def test_shared_selection_updates_existing_rows_without_rebuilding_list(self):
        run_scenario(self, r'''
state.libraryId='a'; state.snapshotId='s';
state.documents=[{document_id:'d1',title:'First'},{document_id:'d2',title:'Second'}];
state.managing=true; state.removalIds=['d1'];
route=path=> {const id=path.split('/').pop(); return response({library_id:'a',snapshot_id:'s',document:{document_id:id,title:id},toc:{items:[]}});};
for(const kind of ['doc-row','search-card']) {
  const list=new Element(); list.scrollTop=123;
  const first=new Element('button'), second=new Element('button'), checkbox=new Element('input');
  checkbox.checked=true;
  for(const [node,id] of [[first,'d1'],[second,'d2']]) {node.dataset={action:'select-document',id}; node.classList.add(kind);}
  if(kind==='search-card') {first.dataset.chunkId='c1'; second.dataset.chunkId='c2';}
  first.classList.add('selected'); list.append(first,second,checkbox); document.body.append(list);
  second.focus();
  for(const fn of events.get('click')) await fn({target:second});
  await drain();
  assert(!first.classList.contains('selected')); assert(second.classList.contains('selected'));
  assert.equal(state.documentDetail.document.document_id,'d2');
  assert.equal(document.activeElement,second); assert.equal(list.scrollTop,123);
  assert.equal(list.children[1],second); assert(checkbox.checked); assert.deepEqual(state.removalIds,['d1']);
  documentMenuTarget={...selectionCapture(),documentId:'d1',row:first};
  documentMenuAction('context-details'); await drain();
  assert.equal(first.classList.contains('selected'),kind==='doc-row'); assert(!second.classList.contains('selected'));
  assert.equal(state.documentDetail.document.document_id,'d1'); assert(checkbox.checked);
  list.remove();
}
''')
