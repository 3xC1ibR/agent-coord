const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {MessageChannel} = require('node:worker_threads');
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
  await f.phone.ready();
  const receive = f.listeners.get('message');
  receive({data: {type: 'agent-coord-open-notification', url: 'https://evil.test/#thread-one'}});
  assert.deepEqual(f.opened, []);
  receive({data: {type: 'agent-coord-open-notification', url: 'https://mac.test.ts.net/#thread-one'}});
  assert.deepEqual(f.opened, ['thread-one']);
});

test('notification is acknowledged and queued until app startup finishes', async () => {
  const f = fixture(), replies = [];
  const receive = f.listeners.get('message');
  receive({data: {type: 'agent-coord-open-notification', url: '/#thread_one.2'},
    ports: [{postMessage: value => replies.push(value)}]});
  assert.equal(replies[0].type, 'agent-coord-notification-received');
  assert.deepEqual(f.opened, []);
  await f.phone.ready();
  assert.deepEqual(f.opened, ['thread_one.2']);
  await f.phone.ready();
  assert.deepEqual(f.opened, ['thread_one.2']);
  for (const url of ['https://evil.test/#thread', '/?navigate=evil#thread', '/#bad/id']) {
    receive({data: {type: 'agent-coord-open-notification', url},
      ports: [{postMessage() {assert.fail('invalid URL acknowledged');}}]});
  }
  assert.deepEqual(f.opened, ['thread_one.2']);
});

function worker() {
  const handlers = new Map(), notifications = [], opened = [], messages = [];
  let windows = [], openedClient = null;
  const self = {location: {origin: 'https://mac.test.ts.net'},
    addEventListener: (name, handler) => handlers.set(name, handler), skipWaiting() {},
    registration: {showNotification: async (...args) => notifications.push(args)},
    clients: {claim: async () => {}, matchAll: async () => windows,
      openWindow: async url => {opened.push(url); return openedClient;}},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../plugins/agent-coord/scripts/agent_coord/web/push-worker.js'), 'utf8'),
    {self, URL, MessageChannel, setTimeout, clearTimeout});
  const dispatch = async (name, event) => {
    let promise;
    handlers.get(name)({...event, waitUntil: value => {promise = value;}});
    await promise;
  };
  return {handlers, dispatch, notifications, opened, messages, setWindows: value => {windows = value;},
    setOpenedClient: value => {openedClient = value;}};
}

test('push displays thread context and safely falls back for malformed payloads; never caches requests', async () => {
  const w = worker();
  assert.equal(w.handlers.has('fetch'), false);
  await w.dispatch('push', {data: {json: () => ({title: 'projects · Turn finished', body: 'Prioritize this week outcomes', url: 'https://evil.test/'})}});
  assert.equal(w.notifications[0][0], 'projects · Turn finished');
  assert.equal(w.notifications[0][1].body, 'Prioritize this week outcomes');
  assert.equal(w.notifications[0][1].data.url, 'https://mac.test.ts.net/');
  await w.dispatch('push', {data: {json() {throw Error('bad JSON');}}});
  assert.equal(w.notifications.length, 2);
  assert.equal(w.notifications[1][0], 'Ribbon Field');
  assert.equal(w.notifications[1][1].body, 'Ribbon Field has an update.');
  await w.dispatch('push', {data: {json: () => ({url: 'https://['})}});
  assert.equal(w.notifications.length, 3);
  await w.dispatch('push', {data: {json: () => ({title: {}, body: 2, url: '/#thread_one.2'})}});
  assert.equal(w.notifications[3][0], 'Ribbon Field');
  assert.equal(w.notifications[3][1].data.url, 'https://mac.test.ts.net/#thread_one.2');
  await w.dispatch('push', {data: {json: () => null}});
  assert.equal(w.notifications.length, 5);
});

test('click focuses an existing app and preserves its document and drafts', async () => {
  const w = worker();
  let focused = false, closed = false;
  w.setWindows([{url: 'https://mac.test.ts.net/?view=work#old', focus: async () => {focused = true;},
    postMessage: (message, ports) => {w.messages.push(message); ports[0].postMessage({type: 'agent-coord-notification-received'});},
    navigate() {assert.fail('must preserve the live document and drafts');}}]);
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

test('focus rejection does not swallow the destination', async () => {
  const w = worker();
  w.setWindows([{url: 'https://mac.test.ts.net/', focus: async () => {throw Error('client waking');},
    postMessage: (message, ports) => {w.messages.push(message); ports[0].postMessage({type: 'agent-coord-notification-received'});}}]);
  await w.dispatch('notificationclick', {notification: {close() {}, data: {url: '/#thread-one'}}});
  assert.equal(w.messages[0].url, 'https://mac.test.ts.net/#thread-one');
  assert.deepEqual(w.opened, []);
});

test('cold client without a message listener navigates to the startup thread URL', async () => {
  const w = worker(), navigated = [];
  w.setWindows([{url: 'https://mac.test.ts.net/', focus: async () => {}, postMessage() {},
    navigate: async url => {navigated.push(url); return {};}}]);
  await w.dispatch('notificationclick', {notification: {close() {}, data: {url: '/#thread-one'}}});
  assert.deepEqual(navigated, ['https://mac.test.ts.net/#thread-one']);
  assert.deepEqual(w.opened, []);
});

test('vanished existing client falls back to a new window', async () => {
  const w = worker();
  w.setWindows([{url: 'https://mac.test.ts.net/', focus: async () => {throw Error('gone');},
    postMessage() {throw Error('gone');}, navigate: async () => {throw Error('gone');}}]);
  await w.dispatch('notificationclick', {notification: {close() {}, data: {url: '/#thread-one'}}});
  assert.deepEqual(w.opened, ['https://mac.test.ts.net/#thread-one']);
});

test('Home Screen launch restoring the root receives the clicked destination', async () => {
  const w = worker(), f = fixture();
  w.setOpenedClient({url: 'https://mac.test.ts.net/', focus: async () => {},
    postMessage: (data, ports) => f.listeners.get('message')({data, ports}),
    navigate() {assert.fail('startup queued the navigation');}});
  await w.dispatch('notificationclick', {notification: {close() {}, data: {url: '/#thread-one'}}});
  assert.deepEqual(f.opened, []);
  await f.phone.ready();
  assert.deepEqual(f.opened, ['thread-one']);
});
