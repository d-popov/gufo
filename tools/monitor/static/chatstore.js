'use strict';

// Where the chat keeps its conversations and projects: this browser's IndexedDB, which holds far
// more than localStorage and is never sent to the monitor. If the browser refuses it (some private
// windows), the same calls work in memory and `persistent` is false, so the page can say so.
(() => {
  const DEFAULT_NAME = 'gufo-chat';
  const TABLES = ['chats', 'projects'];

  function openDatabase(name) {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('no IndexedDB')); return; }
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => {
        for (const table of TABLES) request.result.createObjectStore(table, { keyPath: 'id' });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('IndexedDB is blocked'));
    });
  }

  // One request in its own transaction, settled when the transaction has committed.
  function run(db, table, mode, action) {
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(table, mode);
      let result;
      const request = action(transaction.objectStore(table));
      request.onsuccess = () => { result = request.result; };
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error('The browser refused to store this.'));
    });
  }

  // Moves chats an older version kept in `storage` under `key` into the store, skipping ids it
  // already has, and returns them. The old copy is deleted only once every chat is committed to
  // a store that survives the page: with the browser database unavailable, or any write refused,
  // it stays where it is for the next visit.
  async function migrate(store, storage, key, existing = []) {
    try {
      const old = JSON.parse(storage.getItem(key) || '[]');
      if (!Array.isArray(old)) return [];
      const known = new Set(existing.map((chat) => chat.id));
      const moved = [];
      for (const chat of old) {
        if (!chat || typeof chat.id !== 'string' || !Array.isArray(chat.messages) || known.has(chat.id)) continue;
        await store.put('chats', chat);
        moved.push(chat);
      }
      if (store.persistent) storage.removeItem(key);
      return moved;
    } catch { return []; }  // unreadable or refused: the old copy stays
  }

  async function open(name = DEFAULT_NAME) {
    let db = null;
    try { db = await openDatabase(name); } catch { db = null; }
    const memory = new Map(TABLES.map((table) => [table, new Map()]));
    return {
      persistent: db !== null,
      async getAll(table) {
        return db ? run(db, table, 'readonly', (store) => store.getAll()) : [...memory.get(table).values()];
      },
      async put(table, value) {
        if (db) await run(db, table, 'readwrite', (store) => store.put(value));
        else memory.get(table).set(value.id, structuredClone(value));
      },
      async remove(table, id) {
        if (db) await run(db, table, 'readwrite', (store) => store.delete(id));
        else memory.get(table).delete(id);
      },
    };
  }

  window.GufoStore = { open, migrate };
})();
