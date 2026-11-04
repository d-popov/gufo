'use strict';

// "Share" on the chat page: makes the open chat a room with a public link (see shares.py) and shows it to copy. The links themselves
// are listed and revoked on /shares. Only the admin sees the button. The room offers the tools and thinking effort chosen here: they
// are sent with the link, and again whenever they change while the open chat is shared.
(() => {
  const $ = (id) => document.getElementById(id);
  let host = null;   // { chat(), shared(link), settings() } from chat.js: the open chat, and its room's {tools, effort}
  let pending = null; // the settings update waiting to be sent

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
          ...host.settings(),
          ...(chat ? { chat_id: chat.id, messages: chat.messages.map(({ role, content, error, reasoning, steps, stats }) => ({
            role, content, error, reasoning, stats,
            // The room shows a step as the chat does, without the figures' bytes.
            steps: steps?.map(({ figures, ...step }) => ({ ...step, ...(figures?.length ? { figureCount: figures.length } : {}) })),
          })) } : {}),
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

  // Sends the current tools and effort to the open chat's room, a moment after the last change.
  function update() {
    clearTimeout(pending);
    pending = setTimeout(async () => {
      const id = host?.chat()?.shared?.id;
      if (!id) return;
      try {
        const response = await fetch(`/api/chat/shares/${encodeURIComponent(id)}/settings`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(host.settings()),
        });
        if (!response.ok) throw new Error(`${response.status}`);
      } catch (error) {
        $('status').textContent = `The shared room did not get the new settings (${error.message}).`;
      }
    }, 400);
  }

  window.GufoShare = {
    update,
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
