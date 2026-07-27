(function(){
  const root = document.documentElement;
  const theme = localStorage.getItem('toror-theme');
  if (theme === 'dark') root.classList.add('theme-dark');

  document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
    btn.addEventListener('click', () => {
      const isDark = root.classList.toggle('theme-dark');
      localStorage.setItem('toror-theme', isDark ? 'dark' : 'light');
    });
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  initAuthForms();
  initChat();

  function initAuthForms() {
    document.querySelectorAll('[data-auth-form]').forEach(form => {
      const fields = {
        locationText: form.querySelector('[data-location-text]'),
        locationLat: form.querySelector('[data-location-lat]'),
        locationLng: form.querySelector('[data-location-lng]'),
        deviceInfo: form.querySelector('[data-device-info]'),
      };

      if (fields.deviceInfo) {
        const parts = [navigator.userAgent, navigator.language, Intl.DateTimeFormat().resolvedOptions().timeZone].filter(Boolean);
        fields.deviceInfo.value = parts.join(' | ').slice(0, 500);
      }

      if (!navigator.geolocation) return;
      navigator.geolocation.getCurrentPosition(
        pos => {
          const lat = pos.coords.latitude;
          const lng = pos.coords.longitude;
          if (fields.locationLat) fields.locationLat.value = lat.toFixed(6);
          if (fields.locationLng) fields.locationLng.value = lng.toFixed(6);
          if (fields.locationText) fields.locationText.value = `Lat ${lat.toFixed(5)}, Lng ${lng.toFixed(5)}`;
        },
        () => {},
        { enableHighAccuracy: false, maximumAge: 600000, timeout: 5000 }
      );
    });
  }

  function initChat() {
    const chatForm = document.querySelector('[data-chat-form]');
    const chatList = document.querySelector('[data-chat-list]');
    const typingLine = document.getElementById('typing-line');
    const sendLabel = document.querySelector('[data-send-label]');
    const cancelEdit = document.querySelector('[data-cancel-edit]');
    const editId = document.querySelector('[data-edit-id]');
    const textArea = chatForm ? chatForm.querySelector('textarea[name="message"]') : null;
    const menu = document.getElementById('chat-menu');

    if (!chatForm || !chatList || !textArea || !menu) return;

    let lastId = parseInt(chatList.dataset.lastId || '0', 10);
    let pollBusy = false;
    let typingTimer = null;
    let menuNode = null;
    let menuMsg = null;
    let longPressTimer = null;

    const isAdmin = String(window.__TOROR_IS_ADMIN) === 'true';
    const userEmail = String(window.__TOROR_USER_EMAIL || '');

    function setTyping(show) {
      if (typingLine) typingLine.hidden = !show;
    }

    function setEditState(id, value) {
      editId.value = id || '';
      if (sendLabel) sendLabel.textContent = id ? 'Update' : 'Send';
      if (cancelEdit) cancelEdit.hidden = !id;
      if (typeof value === 'string') textArea.value = value;
      if (id) textArea.focus();
    }

    function canControl(node) {
      return node.dataset.owner === 'true' || isAdmin;
    }

    function formatStamp(stamp) {
      return String(stamp || '').replace('T', ' ').slice(0, 19);
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function closeMenu() {
      menu.hidden = true;
      menuNode = null;
      menuMsg = null;
    }

    function positionMenu(node) {
      const rect = node.getBoundingClientRect();
      menu.style.top = `${Math.max(8, rect.top + window.scrollY - 8)}px`;
      menu.style.left = `${Math.max(8, rect.right + window.scrollX - 170)}px`;
    }

    function openMenu(node) {
      if (!canControl(node)) return;
      menuNode = node;
      menuMsg = node.querySelector('.content')?.textContent || '';
      positionMenu(node);
      menu.hidden = false;
    }

    function wireNode(node) {
      const own = canControl(node);
      const btn = node.querySelector('[data-msg-menu]');
      if (btn) {
        btn.hidden = !own;
        btn.addEventListener('click', e => {
          e.stopPropagation();
          openMenu(node);
        });
      }

      let pressStart = null;
      const startPress = () => {
        if (!own) return;
        clearTimeout(longPressTimer);
        longPressTimer = setTimeout(() => openMenu(node), 560);
      };
      const cancelPress = () => clearTimeout(longPressTimer);
      node.addEventListener('touchstart', startPress, { passive: true });
      node.addEventListener('touchend', cancelPress);
      node.addEventListener('touchmove', cancelPress);
      node.addEventListener('mousedown', startPress);
      node.addEventListener('mouseup', cancelPress);
      node.addEventListener('mouseleave', cancelPress);
      node.addEventListener('click', () => {
        if (menu.hidden) return;
      });
    }

    function addBubble(item) {
      const div = document.createElement('div');
      const isMine = item.sender === userEmail || (isAdmin && item.role === 'admin');
      div.className = 'msg ' + (item.role === 'admin' ? 'admin' : 'visitor') + (isMine ? ' mine' : '');
      div.dataset.messageId = item.id;
      div.dataset.owner = isMine ? 'true' : 'false';
      div.dataset.role = item.role;
      div.innerHTML = `
        <div class="bubble">
          <div class="meta"><span>${escapeHtml(isMine ? 'You' : item.sender)}</span><span>${formatStamp(item.created_at)}</span></div>
          <div class="content"></div>
          <div class="statusline">
            ${item.edited ? '<span>edited</span>' : ''}
            ${isMine && item.is_read ? '<span>seen</span>' : ''}
          </div>
          <button type="button" class="msg-menu-btn" data-msg-menu aria-label="Message options">▾</button>
        </div>`;
      div.querySelector('.content').textContent = item.message;
      wireNode(div);
      chatList.appendChild(div);
      chatList.scrollTop = chatList.scrollHeight;
    }

    async function deleteMessage(id) {
      const res = await fetch(`/api/chat/delete/${id}`, { method: 'POST', headers: { 'X-Requested-With': 'fetch' } });
      if (res.ok) {
        const node = chatList.querySelector(`[data-message-id="${id}"]`);
        if (node) node.remove();
      }
    }

    async function resendMessage(text) {
      const formData = new FormData();
      formData.append('message', text);
      formData.append('edit_id', '');
      await fetch('/api/chat/send', { method: 'POST', body: formData });
    }

    async function poll() {
      if (pollBusy) return setTimeout(poll, 3500);
      pollBusy = true;
      try {
        const res = await fetch(`/api/chat/poll?last_id=${lastId}`, { headers: { 'X-Requested-With': 'fetch' } });
        if (!res.ok) throw new Error('poll failed');
        const items = await res.json();
        if (items.length) {
          for (const item of items) {
            lastId = Math.max(lastId, item.id);
            addBubble(item);
            if (item.role === 'admin') notify(item.message);
          }
        }
      } catch (_) {}
      pollBusy = false;
      setTimeout(poll, 4500);
    }

    function notify(message) {
      if (!('Notification' in window)) return;
      if (Notification.permission === 'default') {
        try { Notification.requestPermission(); } catch (_) {}
      }
      if (Notification.permission === 'granted') {
        try { new Notification('Toror message', { body: String(message || '').slice(0, 120) }); } catch (_) {}
      }
    }

    document.addEventListener('click', e => {
      if (menu.hidden) return;
      if (menu.contains(e.target)) return;
      if (e.target.closest('[data-msg-menu]')) return;
      closeMenu();
    });

    menu.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!menuNode) return;
        const id = menuNode.dataset.messageId;
        const text = menuMsg || '';
        const action = btn.dataset.action;
        closeMenu();
        if (action === 'edit') {
          setEditState(id, text);
        } else if (action === 'resend') {
          await resendMessage(text);
        } else if (action === 'delete') {
          if (confirm('Delete this message?')) await deleteMessage(id);
        }
      });
    });

    chatForm.addEventListener('submit', async e => {
      e.preventDefault();
      const text = textArea.value.trim();
      if (!text) return;
      setTyping(true);
      const formData = new FormData(chatForm);
      try {
        const res = await fetch('/api/chat/send', { method: 'POST', body: formData });
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          const editedId = editId.value;
          if (data.edited && editedId) {
            const node = chatList.querySelector(`[data-message-id="${editedId}"]`);
            if (node) {
              const content = node.querySelector('.content');
              if (content) content.textContent = text;
              const status = node.querySelector('.statusline');
              if (status && !status.textContent.includes('edited')) {
                const span = document.createElement('span');
                span.textContent = 'edited';
                status.prepend(span);
              }
            }
          }
          textArea.value = '';
          setEditState('', '');
        }
      } finally {
        setTyping(false);
      }
    });

    textArea.addEventListener('input', () => {
      clearTimeout(typingTimer);
      setTyping(true);
      typingTimer = setTimeout(() => setTyping(false), 900);
    });

    if (cancelEdit) {
      cancelEdit.addEventListener('click', () => setEditState('', ''));
    }

    document.querySelectorAll('.msg').forEach(wireNode);
    poll();
  }
})();
