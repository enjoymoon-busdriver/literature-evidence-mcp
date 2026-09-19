import unittest
from tests.test_formal_ui_frontend import run_scenario

SETUP = r'''
state.page='documents'; state.libraryId='a'; state.snapshotId='s1';
state.libraries=[{library_id:'a',name:'A'}];
state.snapshots=[{snapshot_id:'s1',verified:true,members:[]}];
state.documents=[{document_id:'d1',title:'Previously selected'},{document_id:'d2',title:'Right clicked'}];
state.selectedDocumentId='d1'; state.managing=true; state.removalIds=['d1'];
const menu=nodes.get('document-menu');
const details=new Element('button'); const remove=new Element('button');
menu.append(details,remove);
menu.querySelector=selector=>selector.includes('context-remove')?remove:details;
menu.querySelectorAll=()=>[details,remove];
menu.getBoundingClientRect=()=>({width:230,height:110});
window.innerWidth=320; window.innerHeight=240;
const row=new Element('button'); row.dataset.id='d2';
row.closest=()=>row; row.getBoundingClientRect=()=>({left:280,bottom:239});
'''


class ContextMenuTests(unittest.TestCase):
    def test_exact_target_scope_single_removal_and_cancel(self):
        run_scenario(self, SETUP + r'''
assert(documentSubtitle({chunk_count:1}).includes('文本片段：1 段'));
openDocumentMenu(row,319,239);
assert.equal(menu.style.left,'82px'); assert.equal(menu.style.top,'122px');
assert.equal(document.activeElement,details);
documentMenuAction('context-remove');
assert.deepEqual(state.removalTarget.ids,['d2']);
assert.deepEqual(state.removalIds,['d1']);
assert(nodes.get('modal-content').innerHTML.includes('本次仅移除右键指定的这一篇'));
assert(nodes.get('modal-content').innerHTML.includes('Right clicked'));
assert(!nodes.get('modal-content').innerHTML.includes('Previously selected'));
const before=requests.length;
for(const fn of events.get('click')) await fn({target:{closest:()=>({dataset:{action:'close-modal'},disabled:false})}});
assert.equal(requests.length,before);
state.removalTarget=null;
openDocumentMenu(row,0,0); state.libraryId='b'; state.libraryEpoch++;
documentMenuAction('context-remove'); assert.equal(state.removalTarget,null);
state.libraryId='a'; openDocumentMenu(row,0,0); clearContentSelection();
assert.equal(documentMenuTarget,null); assert.equal(menu.hidden,true);
state.documents=[{document_id:'d2',title:'Only document'}];
openDocumentMenu(row,0,0); assert.equal(remove.disabled,true);
''')

    def test_keyboard_escape_outside_and_details_use_right_clicked_document(self):
        run_scenario(self, SETUP + r'''
let prevented=false;
for(const fn of events.get('keydown')) fn({target:row,key:'F10',shiftKey:true,preventDefault:()=>{prevented=true;}});
assert(prevented); assert.equal(menu.hidden,false);
for(const fn of events.get('keydown')) fn({target:details,key:'ArrowDown',preventDefault:()=>{}});
assert.equal(document.activeElement,remove);
for(const fn of events.get('keydown')) fn({target:remove,key:'Escape',preventDefault:()=>{}});
assert.equal(menu.hidden,true); assert.equal(document.activeElement,row);
openDocumentMenu(row,0,0);
for(const fn of events.get('pointerdown')) fn({target:document.body});
assert.equal(menu.hidden,true);
openDocumentMenu(row,0,0);
route=(path)=>response({library_id:'a',snapshot_id:'s1',document:{document_id:'d2',title:'Right clicked'},toc:{items:[]}});
documentMenuAction('context-details'); await drain();
assert.equal(state.selectedDocumentId,'d2');
assert(requests.some(item=>item.path.includes('/documents/d2')));
''')
