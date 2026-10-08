const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../plugins/agent-coord/scripts/agent_coord/remote_access.py'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];

function page(hash = '', response = {ok: true, json: async () => ({})}) {
  const elements = {
    name: {value: 'Home Screen'}, 'pair-link': {value: '', required: false},
    'pair-link-field': {hidden: true}, status: {textContent: ''},
  };
  const button = {disabled: false};
  const form = {querySelector: () => button};
  const posts = [], redirects = [], history = [];
  const context = {
    URL, URLSearchParams,
    location: {hash, origin: 'https://mac.test.ts.net', replace: value => redirects.push(value)},
    history: {replaceState: (...args) => history.push(args)},
    document: {querySelector: () => form, getElementById: id => elements[id]},
    fetch: async (url, options) => {posts.push({url, ...options}); return response;},
  };
  vm.runInNewContext(script, context);
  return {elements, button, posts, redirects, history, submit: () => form.onsubmit({preventDefault() {}})};
}

test('invitation opens without keeping its secret in history', async () => {
  const app = page('#pair=invitation');
  assert.equal(app.history[0][2], '/');
  assert.equal(app.elements['pair-link-field'].hidden, true);
  await app.submit();
  assert.equal(app.posts[0].url, '/api/remote/pair');
  assert.deepEqual(JSON.parse(app.posts[0].body), {token: 'invitation', name: 'Home Screen'});
  assert.deepEqual(app.redirects, ['/']);
});

test('unpaired Home Screen launch accepts a pasted same-origin invitation', async () => {
  const app = page();
  assert.equal(app.elements['pair-link-field'].hidden, false);
  assert.equal(app.elements['pair-link'].required, true);
  app.elements['pair-link'].value = ' https://mac.test.ts.net/pair#pair=fresh ';
  await app.submit();
  assert.equal(JSON.parse(app.posts[0].body).token, 'fresh');
  assert.deepEqual(app.redirects, ['/']);
});

test('invalid, foreign, and credential-bearing links never send a pairing request', async () => {
  for (const link of ['nonsense', 'https://other.ts.net/pair#pair=secret',
    'http://mac.test.ts.net/pair#pair=secret', 'https://mac.test.ts.net/#pair=secret',
    'https://mac.test.ts.net/pair', 'https://user:pass@mac.test.ts.net/pair#pair=secret']) {
    const app = page();
    app.elements['pair-link'].value = link;
    await app.submit();
    assert.equal(app.posts.length, 0, link);
    assert.equal(app.button.disabled, false);
    assert.ok(app.elements.status.textContent);
  }
});

test('expired invitation can be replaced inside the installed app', async () => {
  const app = page('', {ok: false, json: async () => ({error: 'Pairing link expired.'})});
  app.elements['pair-link'].value = 'https://mac.test.ts.net/pair#pair=expired';
  await app.submit();
  assert.equal(app.elements.status.textContent, 'Pairing link expired.');
  assert.equal(app.button.disabled, false);
  assert.deepEqual(app.redirects, []);
  app.elements['pair-link'].value = 'https://mac.test.ts.net/pair#pair=fresh';
  await app.submit();
  assert.equal(JSON.parse(app.posts[1].body).token, 'fresh');
});
