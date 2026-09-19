from pathlib import Path
import unittest

from tests.test_formal_ui_frontend import run_scenario
from tests.test_real_connections_frontend import CONNECTIONS


class SearchInteractionTests(unittest.TestCase):
    def test_preview_is_not_selection_and_same_document_chunks_select_individually(self):
        run_scenario(self, r'''
state.libraryId='a'; state.snapshotId='s'; state.page='search';
state.snapshots=[{snapshot_id:'s',verified:true}];
const results=[
 {document_id:'d1',chunk_id:'c1',excerpt:'First'},
 {document_id:'d1',chunk_id:'c2',excerpt:'Second'},
 {document_id:'d2',chunk_id:'c3',excerpt:'Third'},
];
route=path=>response(path.endsWith('/search')
 ? {library_id:'a',snapshot_id:'s',results}
 : {library_id:'a',snapshot_id:'s',document:{document_id:path.split('/').pop()},toc:{items:[]}});
assert(!searchPage().includes('search-card'));
await runSearch('synthetic'); await drain();
assert.equal(state.documentDetail.document.document_id,'d1','first detail preview lost');
assert(!searchPage().includes('search-card selected'),'preview pretended to be a click');
assert.equal((searchPage().match(/aria-pressed="false"/g)||[]).length,3);
const list=new Element(); document.body.append(list);
const cards=results.map(item=>{
 const node=new Element('button'); node.dataset={action:'select-document',id:item.document_id,chunkId:item.chunk_id};
 node.classList.add('search-card'); list.append(node); return node;
});
for(const index of [1,0,2]) {
 cards[index].focus();
 for(const fn of events.get('click')) await fn({target:cards[index]});
 await drain();
 assert.equal(state.search.selectedChunkId,results[index].chunk_id);
 assert.equal(state.documentDetail.document.document_id,results[index].document_id);
 assert.deepEqual(cards.map(c=>c.classList.contains('selected')),cards.map((c,i)=>i===index));
 assert.deepEqual(cards.map(c=>c.getAttribute('aria-pressed')),cards.map((c,i)=>String(i===index)));
 assert.equal((searchPage().match(/search-card selected/g)||[]).length,1);
 assert.equal(document.activeElement,cards[index]);
 // Hover/focus do not dispatch a selection action.
 for(const type of ['mouseover','mouseenter','focusin'])
   for(const fn of events.get(type)||[]) await fn({target:cards[(index+1)%3]});
 assert.equal(state.search.selectedChunkId,results[index].chunk_id);
}
await runSearch('new query'); await drain();
assert(!searchPage().includes('search-card selected'));
clearContentSelection();
assert(!searchPage().includes('search-card'));
assert.equal(state.selectedDocumentId,'');
''')

    def test_switching_modes_preserves_draft_and_only_submit_uses_current_mode(self):
        run_scenario(self, CONNECTIONS + r'''
state.libraryId='a'; state.snapshotId='s'; state.page='search';
state.snapshots=[{snapshot_id:'s',verified:true}];
const connections=connectionStatus();
connections.aliyun_configured=true; connections.model_settings.enabled=true;
FolioConnections.acceptStatus({connections});
const query='尚未提交的容量问题';
const input=nodes.get('search-input'); input.value=query;
for(const fn of events.get('input')) fn({target:input});
route=(path,options)=>response({library_id:'a',snapshot_id:'s',results:[]});
for(const mode of ['enhanced','bm25']) {
 const before=requests.length;
 for(const fn of events.get('change')) fn({target:{id:'search-mode',value:mode}});
 assert.equal(requests.length,before,'switch triggered a search/provider request');
 assert.equal(nodes.get('search-input').value,query);
 assert.equal(state.search.query,query);
 assert.equal(state.search.ran,false);
 for(const fn of events.get('submit')) fn({target:{id:'search-form'},preventDefault(){}});
 await drain();
 assert.equal(requests.length,before+1);
 assert.equal(JSON.parse(requests.at(-1).options.body).mode,mode);
 assert.equal(JSON.parse(requests.at(-1).options.body).query,query);
}
''')

    def test_hover_selected_and_keyboard_focus_have_distinct_styles(self):
        css=(Path(__file__).resolve().parents[1]/'src/literature_evidence_mcp/static/styles.css').read_text()
        self.assertIn('.search-card:hover { background: #f5f6f8;',css)
        self.assertIn('.search-card.selected { background: #e1edff;',css)
        self.assertIn('box-shadow: inset 4px 0 var(--blue)',css)
        self.assertIn('.search-card:focus-visible { outline: 3px solid #175cd3;',css)
