const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const PhoneNotifications = require('../plugins/agent-coord/scripts/agent_coord/web/push-notifications.js');

function fixture() {
  const element = () => ({disabled: false, hidden: false, textContent: '', setAttribute() {}, after() {}});
  const calls = [], opened = [], listeners = new Map();
  let permissionRequests = 0, subscription = null, enabled = false;
  const env = {
    document: {createElement: element}, isSecureContext: true, PushManager: {}, atob,
    location: {origin: 'https://mac.test.ts.net'},
    addEventListener() {},
    Notification: {permission: 'default', async requestPermission() {
      permissionRequests++; this.permission = 'granted'; return 'granted';
    }},
    navigator: {serviceWorker: {
      addEventListener: (name, handler) => listeners.set(name, handler), removeEventListener() {},
      register: async () => calls.push('register'),
    }},
  };
  const registration = {pushManager: {
    getSubscription: async () => subscription,
    subscribe: async options => {
      calls.push('subscribe');
      assert.equal(options.userVisibleOnly, true);
      subscription = {toJSON: () => ({endpoint: 'https://web.push.apple.com/test', keys: {}}),
        unsubscribe: async () => {calls.push('unsubscribe'); subscription = null;}};
      return subscription;
    },
  }};
  env.navigator.serviceWorker.ready = Promise.resolve(registration);
  const api = async (route, body) => {
    calls.push(route);
    if (route === 'push/subscribe') {assert.ok(body.subscription); enabled = true;}
    if (route === 'push/unsubscribe') enabled = false;
    return {available: true, publicKey: 'BAAA', enabled};
  };
  const button = element();
  const phone = new PhoneNotifications({button, api, openThread: id => opened.push(id), onError: error => {throw error;}}, env);
  return {phone, button, env, calls, opened, listeners, permissionRequests: () => permissionRequests};
}

test('permission is requested only on a tap; subscriptions go to authenticated API', async () => {
  const f = fixture();
  await f.phone.start();
  assert.equal(f.permissionRequests(), 0);
  await f.phone.toggle();
  assert.equal(f.permissionRequests(), 1);
  assert.equal(f.button.textContent, 'Phone notifications on');
  assert.equal(f.phone.testButton.hidden, false);
  assert.ok(f.calls.includes('push/subscribe'));
  await f.phone.receive([{status: 'completed'}]);
  assert.ok(!f.calls.includes('notifications/claim'));
});

test('navigation can sync phone focus without desktop notification side effects', async () => {
  for (const supported of [true, false]) {
    const f = fixture();
    if (!supported) delete f.env.PushManager;
    assert.doesNotThrow(() => f.phone.syncFocus());
    await f.phone.start();
    if (supported) await f.phone.toggle();
    const calls = [...f.calls], permissionRequests = f.permissionRequests();
    // Thread selection and returning home both call this shared interface.
    assert.doesNotThrow(() => f.phone.syncFocus());
    assert.doesNotThrow(() => f.phone.syncFocus());
    assert.deepEqual(f.calls, calls);
    assert.equal(f.permissionRequests(), permissionRequests);
  }
});

test('disabling removes server subscription before browser subscription', async () => {
  const f = fixture();
  await f.phone.start();
  await f.phone.toggle();
  await f.phone.toggle();
  assert.ok(f.calls.indexOf('push/unsubscribe') < f.calls.indexOf('unsubscribe'));
  assert.equal(f.phone.testButton.hidden, true);
});

test('unsupported iPhone tab explains Home Screen installation', async () => {
  const f = fixture();
  delete f.env.PushManager;
  await f.phone.start();
  assert.equal(f.button.disabled, true);
  assert.match(f.phone.message.textContent, /Home Screen/);
  assert.equal(f.permissionRequests(), 0);
});

test('worker messages open same-origin conversations without navigating away', async () => {
  const f = fixture();
  const receive = f.listeners.get('message');
  receive({data: {type: 'agent-coord-open-notification', url: 'https://evil.test/#thread-one'}});
  assert.deepEqual(f.opened, []);
  receive({data: {type: 'agent-coord-open-notification', url: 'https://mac.test.ts.net/#thread-one'}});
  assert.deepEqual(f.opened, ['thread-one']);
});

function worker() {
  const handlers = new Map(), notifications = [], opened = [], messages = [];
  let windows = [];
  const self = {location: {origin: 'https://mac.test.ts.net'},
    addEventListener: (name, handler) => handlers.set(name, handler), skipWaiting() {},
    registration: {showNotification: async (...args) => notifications.push(args)},
    clients: {claim: async () => {}, matchAll: async () => windows, openWindow: async url => opened.push(url)},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../plugins/agent-coord/scripts/agent_coord/web/push-worker.js'), 'utf8'), {self, URL});
  const dispatch = async (name, event) => {
    let promise;
    handlers.get(name)({...event, waitUntil: value => {promise = value;}});
    await promise;
  };
  return {handlers, dispatch, notifications, opened, messages, setWindows: value => {windows = value;}};
}

test('push always shows generic text, including malformed payloads; never caches requests', async () => {
  const w = worker();
  assert.equal(w.handlers.has('fetch'), false);
  await w.dispatch('push', {data: {json: () => ({body: 'Secret prompt', url: 'https://evil.test/'})}});
  assert.equal(w.notifications[0][0], 'Ribbon Field');
  assert.equal(w.notifications[0][1].body, 'Ribbon Field has an update.');
  assert.equal(w.notifications[0][1].data.url, 'https://mac.test.ts.net/');
  await w.dispatch('push', {data: {json() {throw Error('bad JSON');}}});
  assert.equal(w.notifications.length, 2);
  await w.dispatch('push', {data: {json: () => ({url: 'https://['})}});
  assert.equal(w.notifications.length, 3);
});

test('click focuses an existing app and preserves its document and drafts', async () => {
  const w = worker();
  let focused = false, closed = false;
  w.setWindows([{url: 'https://mac.test.ts.net/#old', focus: async () => {focused = true;},
    postMessage: message => w.messages.push(message)}]);
  await w.dispatch('notificationclick', {notification: {close: () => {closed = true;}, data: {url: '/#thread-one'}}});
  assert.equal(focused, true);
  assert.equal(closed, true);
  assert.equal(w.messages[0].url, 'https://mac.test.ts.net/#thread-one');
  assert.deepEqual(w.opened, []);
});

test('click with no app open launches the matching conversation', async () => {
  const w = worker();
  await w.dispatch('notificationclick', {notification: {close() {}, data: {url: '/#thread-one'}}});
  assert.deepEqual(w.opened, ['https://mac.test.ts.net/#thread-one']);
});
