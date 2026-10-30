'use strict';

// Keeps this browser's chats and projects in step with the monitor's copy, so one account sees
// the same ones on every device. The browser's own database stays the working copy and the page
// never waits for the network: a change is marked, pushed a moment later, and the monitor's
// changes since the last revision seen are pulled now and then. Two edits of one item: the later
// stamp wins, on the monitor and here. This file has no page or storage of its own (everything
// comes in through `create`), so tests/tools/chatsync_test.js runs it as it is.
(() => {
  const PUSH_DELAY_MS = 1500;

  function create({ owner, storage, request, list, read, stamp, apply, drop, busy = () => false,
    now = () => Date.now(), later = (fn, ms) => setTimeout(fn, ms), onChange = () => {}, onError = () => {} }) {
    const key = `gufo-chat-sync-${owner}`;
    let state = { rev: null, dirty: {}, gone: {} };
    try {
      const saved = JSON.parse(storage.getItem(key));
      if (saved && typeof saved === 'object') state = { rev: saved.rev ?? null, dirty: saved.dirty ?? {}, gone: saved.gone ?? {} };
    } catch { /* nothing saved yet */ }
    let enabled = true;
    let running = null;
    let timer = null;

    const save = () => { try { storage.setItem(key, JSON.stringify(state)); } catch { /* storage full: the next run starts over */ } };
    const name = (kind, id) => `${kind}:${id}`;
    const split = (value) => { const at = value.indexOf(':'); return [value.slice(0, at), value.slice(at + 1)]; };

    function schedule() {
      if (!enabled || timer !== null) return;
      timer = later(() => { timer = null; sync().catch(() => {}); }, PUSH_DELAY_MS);
    }

    // This item changed here: send it soon.
    function touch(kind, id) {
      if (!enabled) return;
      state.dirty[name(kind, id)] = 1;
      delete state.gone[name(kind, id)];
      save();
      schedule();
    }

    // This item was deleted here, on purpose: tell the monitor.
    function forget(kind, id) {
      if (!enabled) return;
      delete state.dirty[name(kind, id)];
      state.gone[name(kind, id)] = now();
      save();
      schedule();
    }

    function remote(item, changes, defer = () => {}) {
      const id = item.id;
      const current = stamp(item.kind, id);
      const group = item.kind === 'chat' ? 'chats' : 'projects';
      if (state.dirty[name(item.kind, id)] && current !== null && current >= item.updated) return;  // our edit is the later one
      if (item.deleted) {
        if (current !== null) {
          if (busy(item.kind, id)) { defer(); return; }
          drop(item.kind, id);
          changes.removed.push(`${item.kind}:${id}`);
        }
        return;
      }
      if (current !== null && current >= item.updated) return;  // already have it, or newer
      if (busy(item.kind, id)) { defer(); return; }  // a reply is being written into it: this is read again later
      apply(item.kind, id, item.data);
      changes[group].push(id);
    }

    async function call(method, path, body) {
      const reply = await request(method, path, body);
      if (reply.status === 404) { enabled = false; throw new Error('sync is off on the monitor'); }
      return reply;
    }

    async function push(changes) {
      for (const entry of Object.keys(state.dirty)) {
        const [kind, id] = split(entry);
        const item = read(kind, id);
        if (item === null) { delete state.dirty[entry]; continue; }  // nothing worth keeping (an empty chat)
        const reply = await call('PUT', `/api/chat/items/${kind}/${encodeURIComponent(id)}`, { updated: item.updated, data: item.data });
        if (reply.status === 413) {
          delete state.dirty[entry];
          onError(reply.json?.error || 'The monitor refused this item: it is too large or your storage there is full.');
          continue;
        }
        if (reply.status !== 200) throw new Error(`push failed (${reply.status})`);
        delete state.dirty[entry];
        if (reply.json.applied === false) remote(reply.json.item, changes);  // the monitor had a newer one
        save();
      }
      for (const [entry, at] of Object.entries(state.gone)) {
        const [kind, id] = split(entry);
        const reply = await call('DELETE', `/api/chat/items/${kind}/${encodeURIComponent(id)}?at=${at}`);
        if (reply.status !== 200) throw new Error(`delete failed (${reply.status})`);
        delete state.gone[entry];
        if (reply.json.applied === false) remote(reply.json.item, changes);  // edited elsewhere since
        save();
      }
    }

    async function pull(changes) {
      let cursor = state.rev ?? 0;
      let hold = null;  // the first item left unapplied: the next pull starts just before it
      for (;;) {
        const reply = await call('GET', `/api/chat/sync?since=${cursor}`);
        if (reply.status !== 200) throw new Error(`pull failed (${reply.status})`);
        for (const item of reply.json.items) {
          remote(item, changes, () => { hold = hold === null ? item.rev : Math.min(hold, item.rev); });
        }
        cursor = reply.json.through;
        if (!reply.json.more) break;
      }
      state.rev = hold === null ? cursor : Math.min(cursor, hold - 1);
      save();
    }

    async function run() {
      if (!enabled) return null;
      const changes = { chats: [], projects: [], removed: [] };
      if (state.rev === null) {  // the first time: everything here is new to the monitor
        for (const { kind, id } of list()) state.dirty[name(kind, id)] = 1;
        save();
      }
      try {
        await push(changes);
        await pull(changes);
      } finally {
        if (changes.chats.length || changes.projects.length || changes.removed.length) onChange(changes);
      }
      return changes;
    }

    // One run at a time; a request for another while one is under way follows it.
    function sync() {
      const next = (running ?? Promise.resolve()).catch(() => {}).then(run);
      running = next;
      next.catch(() => {}).then(() => { if (running === next) running = null; });
      return next;
    }

    return { touch, forget, sync, get enabled() { return enabled; }, get pending() { return Object.keys(state.dirty).length + Object.keys(state.gone).length; } };
  }

  window.GufoSync = { create };
})();
