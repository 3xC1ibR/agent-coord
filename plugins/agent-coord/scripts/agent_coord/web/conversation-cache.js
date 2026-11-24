"use strict";

// Only authenticated, bounded recent snapshots go to disk. Live provider state
// and rendered elements stay with the document that owns the conversation.
class ConversationSnapshotCache {
  static byteLimit = 32 * 1024 * 1024;

  // JSON escaping uses at most six bytes per UTF-16 code unit. Count final
  // keys and values without copying or scanning multi-megabyte output strings.
  static retainedBytes(detail) {
    if (detail.snapshotBytes > ConversationSnapshotCache.byteLimit) return detail.snapshotBytes;
    let bytes = 0;
    function count(value) {
      if (bytes > ConversationSnapshotCache.byteLimit) return;
      if (typeof value === "string") { bytes += 2 + 6 * value.length; return; }
      if (value === null || typeof value !== "object") { bytes += 32; return; }
      bytes += 2;
      if (Array.isArray(value)) {
        for (const item of value) { bytes++; count(item); if (bytes > ConversationSnapshotCache.byteLimit) break; }
      } else {
        for (const key of Object.keys(value)) {
          bytes += 4 + 6 * key.length;
          count(value[key]);
          if (bytes > ConversationSnapshotCache.byteLimit) break;
        }
      }
    }
    count(detail);
    return Math.max(bytes, Number(detail.snapshotBytes) || 0);
  }

  constructor(scope, database = globalThis.indexedDB) {
    this.scope = scope;
    this.database = database;
  }

  get ready() {
    return this.opening ||= new Promise(resolve => {
      if (!this.scope || !this.database) { resolve(null); return; }
      let request;
      try { request = this.database.open("ribbon-field-conversations:" + this.scope, 2); }
      catch { resolve(null); return; }
      request.onupgradeneeded = event => {
        // Earlier releases stored rows without charging the final representation.
        if (event?.oldVersion === 1) {
          request.result.deleteObjectStore("recent");
          request.result.deleteObjectStore("sizes");
        }
        request.result.createObjectStore("recent", {keyPath: "id"}).createIndex("visited", "visited");
        request.result.createObjectStore("sizes", {keyPath: "id"}).createIndex("visited", "visited");
      };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); this.opening = null; };
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

  async remove(id) {
    const db = await this.ready;
    if (!db) return;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["recent", "sizes"], "readwrite");
      tx.objectStore("recent").delete(id);
      tx.objectStore("sizes").delete(id);
      tx.oncomplete = resolve;
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  }

  async write(id, detail) {
    const db = await this.ready;
    if (!db || detail.thread.historyUnavailable || detail.transferReadSource === "saved") return;
    const turns = detail.thread.turns || [];
    if (turns.length > 40) return; // A paginated/expanded view is not a recent snapshot.
    const bytes = ConversationSnapshotCache.retainedBytes(detail) + 64 + 6 * id.length;
    if (bytes > ConversationSnapshotCache.byteLimit) return this.remove(id);
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["recent", "sizes"], "readwrite"), store = tx.objectStore("recent");
      const visited = Date.now();
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
