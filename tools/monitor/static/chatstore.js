'use strict';

// Where the chat keeps its conversations and projects: this browser's IndexedDB, which holds far
// more than localStorage and is never sent to the monitor. If the browser refuses it (some private
// windows), the same calls work in memory and `persistent` is false, so the page can say so.
(() => {
  const NAME = 'gufo-chat';
  const TABLES = ['chats', 'projects'];

  function openDatabase() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('no IndexedDB')); return; }
      const request = indexedDB.open(NAME, 1);
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

  async function open() {
    let db = null;
    try { db = await openDatabase(); } catch { db = null; }
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

  window.GufoStore = { open };
})();
