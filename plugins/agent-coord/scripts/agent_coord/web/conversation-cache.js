"use strict";

// Only authenticated, bounded recent snapshots go to disk. Live provider state
// and rendered elements stay with the document that owns the conversation.
class ConversationSnapshotCache {
  constructor(scope, database = globalThis.indexedDB) {
    this.ready = new Promise(resolve => {
      if (!scope || !database) { resolve(null); return; }
      const request = database.open("ribbon-field-conversations:" + scope, 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore("recent", {keyPath: "id"}).createIndex("visited", "visited");
        request.result.createObjectStore("sizes", {keyPath: "id"}).createIndex("visited", "visited");
      };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => db.close();
        resolve(db);
      };
      request.onerror = request.onblocked = () => resolve(null);
    });
  }

  async read(id) {
    const db = await this.ready;
    if (!db) return null;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["recent", "sizes"], "readwrite"), store = tx.objectStore("recent");
      const request = store.get(id);
      let detail = null;
      request.onsuccess = () => {
        if (!request.result) return;
        detail = request.result.detail;
        const visited = Date.now();
        tx.objectStore("sizes").put({id, visited, bytes: request.result.bytes});
      };
      tx.oncomplete = () => resolve(detail);
      tx.onerror = tx.onabort = () => reject(tx.error);
    });
  }

  async write(id, detail) {
    const db = await this.ready;
    if (!db || detail.thread.historyUnavailable || detail.transferReadSource === "saved") return;
    const turns = detail.thread.turns || [];
    if (turns.length > 40) return; // A paginated/expanded view is not a recent snapshot.
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["recent", "sizes"], "readwrite"), store = tx.objectStore("recent");
      const bytes = detail.snapshotBytes || 1024 * 1024, visited = Date.now();
      const sizes = tx.objectStore("sizes");
      store.put({id, detail, visited, bytes});
      sizes.put({id, visited, bytes});
      const request = sizes.index("visited").getAll();
      request.onsuccess = () => {
        const rows = request.result;
        let remaining = rows.reduce((total, row) => total + row.bytes, 0), count = rows.length;
        for (const row of rows) {
          if (count <= 64 && remaining <= 32 * 1024 * 1024) break;
          store.delete(row.id); sizes.delete(row.id);
          remaining -= row.bytes; --count;
        }
      };
      tx.oncomplete = resolve;
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  }
}
