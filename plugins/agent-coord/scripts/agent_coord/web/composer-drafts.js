"use strict";
(() => {
  const databaseName = "ribbon-field-composer";
  const fields = ["text", "mentions", "images"];
  const empty = () => ({text: "", mentions: [], images: []});
  const sameImages = (left, right) => left.length === right.length &&
    left.every((image, index) => image.name === right[index].name && image.url === right[index].url);
  const sameMentions = (left, right) => JSON.stringify(left) === JSON.stringify(right);
  const has = (object, field) => Object.hasOwn(object, field);

  function create({scope, indexedDB = globalThis.indexedDB} = {}) {
    if (typeof scope !== "string" || !scope) throw new TypeError("A composer storage scope is required.");
    let opening;
    function open() {
      if (opening) return opening;
      if (!indexedDB) return Promise.reject(new Error("Composer storage is unavailable."));
      opening = new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName, 1);
        request.onupgradeneeded = () => {
          const db = request.result;
          // Typing touches only the small text record, even with several large attachments.
          db.createObjectStore("composers");
          db.createObjectStore("attachments");
          const submissions = db.createObjectStore("submissions", {autoIncrement: true});
          submissions.createIndex("composer", ["scope", "id"]);
          submissions.createIndex("request", ["scope", "id", "requestId"], {unique: true});
        };
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => { db.close(); opening = undefined; };
          db.onclose = () => { opening = undefined; };
          resolve(db);
        };
      }).catch(error => { opening = undefined; throw error; });
      return opening;
    }

    async function transact(stores, mode, operation) {
      const db = await open();
      return new Promise((resolve, reject) => {
        const transaction = mode === "readwrite"
          ? db.transaction(stores, mode, {durability: "strict"})
          : db.transaction(stores, mode);
        let result;
        transaction.oncomplete = () => resolve(result);
        transaction.onabort = () => reject(transaction.error || new Error("Composer storage was interrupted."));
        const perform = callback => {
          try { callback(); }
          catch (error) { transaction.abort(); reject(error); }
        };
        const store = name => transaction.objectStore(name);
        const read = (requests, callback) => {
          let remaining = requests.length;
          for (const request of requests) request.onsuccess = () => {
            if (--remaining === 0) perform(() => callback(...requests.map(item => item.result)));
          };
        };
        perform(() => operation(store, read, value => { result = value; }));
      });
    }

    const composerStores = ["composers", "attachments"];
    const journalStores = [...composerStores, "submissions"];
    function keyFor(id) {
      if (typeof id !== "string" || !id) throw new TypeError("A composer identifier is required.");
      return [scope, id];
    }
    function requestKey(id, requestId) {
      if (typeof requestId !== "string" || !requestId) throw new TypeError("A submission identifier is required.");
      return [...keyFor(id), requestId];
    }
    const requestsFor = (store, key) => composerStores.map(name => store(name).get(key));
    const record = (composer, images) => ({...empty(), ...composer, images: images || []});
    function setImages(store, key, images) {
      store("attachments").put(images, key);
    }
    function pendingRecord({requestId, text, images, mentions, body, path, bodyUsesImages}) {
      return {requestId, text, images, mentions, body: bodyUsesImages ? {...body, images} : body, path};
    }

    return {
      read(id) {
        const key = keyFor(id);
        return transact(composerStores, "readonly", (store, read, end) => {
          read(requestsFor(store, key), (composer, images) => end(composer || images ? record(composer, images) : null));
        });
      },
      write(id, patch) {
        const key = keyFor(id);
        const composerChanged = has(patch, "text") || has(patch, "mentions");
        const imagesChanged = has(patch, "images");
        const stores = [composerChanged && "composers", imagesChanged && "attachments"].filter(Boolean);
        if (!stores.length) return Promise.resolve();
        return transact(stores, "readwrite", (store, read) => {
          if (imagesChanged) setImages(store, key, patch.images);
          if (composerChanged) read([store("composers").get(key)], composer => {
            const previous = composer || {text: "", mentions: []};
            store("composers").put({
              text: has(patch, "text") ? patch.text : previous.text,
              mentions: has(patch, "mentions") ? patch.mentions : previous.mentions,
            }, key);
          });
        });
      },
      // A saved desktop snapshot predates IndexedDB. Import once without
      // resurrecting anything explicitly removed from the current composer.
      seed(id, values) {
        const key = keyFor(id);
        return transact(composerStores, "readwrite", (store, read, end) => {
          read(requestsFor(store, key), (composer, images) => {
            if (composer || images) { end(record(composer, images)); return; }
            const next = Object.fromEntries(fields.map(field => [field, values[field] ?? empty()[field]]));
            store("composers").put({text: next.text, mentions: next.mentions}, key);
            setImages(store, key, next.images);
            end(next);
          });
        });
      },
      stage(id, requestId, {text, images = [], mentions = [], body, path}) {
        const key = keyFor(id), identifier = requestKey(id, requestId);
        return transact(journalStores, "readwrite", (store, read, end) => {
          const journal = store("submissions");
          read([...requestsFor(store, key), journal.index("request").get(identifier)],
            (composer, savedImages, existing) => {
              if (existing) { end(pendingRecord(existing)); return; }
              const bodyUsesImages = Array.isArray(body?.images) && sameImages(body.images, images);
              const submission = {scope, id, requestId, text, images, mentions,
                body: bodyUsesImages ? Object.fromEntries(Object.entries(body).filter(([name]) => name !== "images")) : body,
                bodyUsesImages, path};
              journal.add(submission);
              if (!composer || composer.text === text && sameMentions(composer.mentions, mentions)) {
                store("composers").put({text: "", mentions: []}, key);
              }
              if (savedImages && images.length) {
                const remaining = [...savedImages];
                for (const image of images) {
                  const index = remaining.findIndex(candidate => sameImages([candidate], [image]));
                  if (index >= 0) remaining.splice(index, 1);
                }
                if (remaining.length !== savedImages.length) setImages(store, key, remaining);
              }
              end(pendingRecord(submission));
            });
        });
      },
      pending(id) {
        const key = keyFor(id);
        return transact(["submissions"], "readonly", (store, read, end) =>
          read([store("submissions").index("composer").getAll(key)], rows => end(rows.map(pendingRecord))));
      },
      acknowledge(id, requestId) {
        const identifier = requestKey(id, requestId);
        return transact(["submissions"], "readwrite", (store, read) => {
          const journal = store("submissions");
          read([journal.index("request").getKey(identifier)], primary => {
            if (primary !== undefined) journal.delete(primary);
          });
        });
      },
      recover(id, requestId) {
        const key = keyFor(id), identifier = requestKey(id, requestId);
        return transact(journalStores, "readwrite", (store, read, end) => {
          const journal = store("submissions"), index = journal.index("request");
          read([...requestsFor(store, key), index.get(identifier), index.getKey(identifier)],
            (composer, images, submission, primary) => {
              const saved = record(composer, images);
              if (!submission ||
                  (saved.text || saved.mentions.length) && (saved.text !== submission.text || !sameMentions(saved.mentions, submission.mentions)) ||
                  saved.images.length && !sameImages(saved.images, submission.images)) {
                end(false); return;
              }
              store("composers").put({text: submission.text, mentions: submission.mentions}, key);
              setImages(store, key, submission.images);
              journal.delete(primary);
              end(true);
            });
        });
      },
    };
  }

  globalThis.ComposerDrafts ||= {create};
})();
