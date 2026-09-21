(() => {
  const publicDrawer = document.getElementById('public-drawer');
  const publicToggle = document.querySelector('[data-public-menu]');
  const publicClose = document.querySelectorAll('[data-public-close]');
  const scrim = document.querySelector('.drawer-scrim');
  function setPublicMenu(open) {
    if (!publicDrawer) return;
    publicDrawer.classList.toggle('open', open);
    if (scrim) scrim.classList.toggle('open', open);
    publicDrawer.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (publicToggle) publicToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if (publicToggle) publicToggle.addEventListener('click', () => setPublicMenu(true));
  publicClose.forEach(el => el.addEventListener('click', () => setPublicMenu(false)));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setPublicMenu(false); });
  if (publicDrawer) publicDrawer.querySelectorAll('a').forEach(a => a.addEventListener('click', () => setPublicMenu(false)));

  const adminMenu = document.querySelector('[data-admin-menu]');
  const adminSidebar = document.querySelector('.admin-sidebar');
  if (adminMenu && adminSidebar) {
    adminMenu.addEventListener('click', () => adminSidebar.classList.toggle('open'));
    adminSidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', () => adminSidebar.classList.remove('open')));
  }

  // Existing admin chat remains functional; public visitors no longer use it.
  initChat();

  function initChat() {
    const chatForm = document.querySelector('[data-chat-form]');
    const chatList = document.querySelector('[data-chat-list]');
    if (!chatForm || !chatList) return;
    const textArea = chatForm.querySelector('textarea[name="message"]');
    const editId = chatForm.querySelector('[data-edit-id]');
    const sendLabel = chatForm.querySelector('[data-send-label]');
    const cancelEdit = chatForm.querySelector('[data-cancel-edit]');
    const menu = document.getElementById('chat-menu');
    if (!textArea || !menu) return;
    let lastId = parseInt(chatList.dataset.lastId || '0', 10);
    let currentNode = null;
    let currentText = '';
    function escapeHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
    function closeMenu(){menu.hidden=true;currentNode=null;currentText=''}
    function wire(node){
      const own=node.dataset.owner==='true'; const btn=node.querySelector('[data-msg-menu]');
      if(btn){btn.hidden=!own;btn.addEventListener('click',e=>{e.stopPropagation();currentNode=node;currentText=node.querySelector('.content')?.textContent||'';menu.hidden=false})}
      let timer; const start=()=>{if(!own)return;clearTimeout(timer);timer=setTimeout(()=>{currentNode=node;currentText=node.querySelector('.content')?.textContent||'';menu.hidden=false},560)}; const cancel=()=>clearTimeout(timer);
      node.addEventListener('touchstart',start,{passive:true});node.addEventListener('touchend',cancel);node.addEventListener('touchmove',cancel);node.addEventListener('mousedown',start);node.addEventListener('mouseup',cancel);node.addEventListener('mouseleave',cancel);
    }
    async function add(item){const div=document.createElement('div');const admin=item.role==='admin';div.className='msg '+(admin?'admin':'visitor')+(item.sender===window.__TOROR_USER_EMAIL?' mine':'');div.dataset.messageId=item.id;div.dataset.owner='true';div.dataset.role=item.role;div.innerHTML=`<div class="bubble"><div class="meta"><span>${escapeHtml(item.sender)}</span><span>${escapeHtml(String(item.created_at||'').replace('T',' ').slice(0,19))}</span></div><div class="content">${escapeHtml(item.message)}</div><div class="statusline"></div><button type="button" class="msg-menu-btn" data-msg-menu>▾</button></div>`;chatList.appendChild(div);wire(div);chatList.scrollTop=chatList.scrollHeight}
    async function poll(){try{const res=await fetch(`/api/chat/poll?last_id=${lastId}`,{headers:{'X-Requested-With':'fetch'}});if(res.ok){const items=await res.json();for(const item of items){lastId=Math.max(lastId,item.id);await add(item)}}}catch(_){ }setTimeout(poll,4500)}
    menu.querySelectorAll('button[data-action]').forEach(b=>b.addEventListener('click',async()=>{if(!currentNode)return;const action=b.dataset.action;const id=currentNode.dataset.messageId;textArea.value=currentText;closeMenu();if(action==='edit'){if(editId)editId.value=id;if(sendLabel)sendLabel.textContent='Update';if(cancelEdit)cancelEdit.hidden=false;textArea.focus()}else if(action==='resend'){const fd=new FormData();fd.append('message',currentText);fd.append('edit_id','');await fetch('/api/chat/send',{method:'POST',body:fd})}else if(action==='delete'){if(confirm('Delete this message?'))await fetch('/api/chat/delete/'+id,{method:'POST'})}}));
    document.addEventListener('click',e=>{if(!menu.hidden&&!menu.contains(e.target)&&!e.target.closest('[data-msg-menu]'))closeMenu()});
    chatForm.addEventListener('submit',async e=>{e.preventDefault();const text=textArea.value.trim();if(!text)return;const fd=new FormData(chatForm);const res=await fetch('/api/chat/send',{method:'POST',body:fd});if(res.ok){textArea.value='';if(editId)editId.value='';if(sendLabel)sendLabel.textContent='Send';if(cancelEdit)cancelEdit.hidden=true}});
    if(cancelEdit)cancelEdit.addEventListener('click',()=>{if(editId)editId.value='';textArea.value='';if(sendLabel)sendLabel.textContent='Send';cancelEdit.hidden=true});
    chatList.querySelectorAll('.msg').forEach(wire);poll();
  }
})();
