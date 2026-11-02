'use strict';

// "Share" on the chat page: makes the open chat a room with a public link (see shares.py) and shows it to copy. The links themselves
// are listed and revoked on /shares. Only the admin sees the button.
(() => {
  const $ = (id) => document.getElementById(id);
  let host = null;   // { chat() } from chat.js: the open chat

  async function create() {
    const chat = host?.chat();
    $('share-create').disabled = true;
    $('share-state').textContent = 'Making the link…';
    try {
      const response = await fetch('/api/chat/shares', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: $('share-title').value || chat?.title || 'Shared chat',
          days: Number($('share-days').value),
          max_messages: Number($('share-messages').value),
          max_audio: Number($('share-audio').value),
          ...(chat ? { chat_id: chat.id, messages: chat.messages.map(({ role, content, error }) => ({ role, content, error })) } : {}),
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.error === 'string' ? body.error : `${response.status}`);
      $('share-url').value = body.url;
      $('share-result').hidden = false;
      $('share-state').textContent = '';
      $('share-url').select();
      host.shared?.({ id: body.id, url: body.url });
    } catch (error) {
      $('share-state').textContent = error.message;
    }
    $('share-create').disabled = false;
  }

  window.GufoShare = {
    init(options) {
      host = options;
      $('share-chat').hidden = false;
      $('share-chat').replaceChildren(window.GufoIcons.svg('link'), 'Share');
      $('share-chat').addEventListener('click', () => {
        const panel = $('share-panel');
        panel.hidden = !panel.hidden;
        $('share-chat').setAttribute('aria-expanded', String(!panel.hidden));
        if (!panel.hidden) {
          $('share-title').value = host.chat()?.title ?? '';
          $('share-result').hidden = true;
          $('share-state').textContent = '';
        }
      });
      $('share-close').addEventListener('click', () => { $('share-panel').hidden = true; $('share-chat').setAttribute('aria-expanded', 'false'); });
      $('share-create').addEventListener('click', create);
      $('share-copy').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText($('share-url').value); $('share-copy').textContent = 'Copied'; } catch { $('share-url').select(); }
        setTimeout(() => { $('share-copy').textContent = 'Copy'; }, 1500);
      });
    },
  };
})();
