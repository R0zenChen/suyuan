const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { TextDecoder } = require('node:util');

const source = fs.readFileSync('browser-extension/resolve.js', 'utf8');

async function caseRun(resolved) {
  const nodes = new Map();
  const node = (id) => nodes.get(id) || nodes.set(id, { textContent: '', href: '', children: [], append(item) { this.children.push(item); } }).get(id);
  let opened = '';
  const context = {
    URL, URLSearchParams, TextDecoder, AbortController, Date, Promise, setTimeout, clearTimeout,
    location: { search: '?doi=10.1000%2Fxyz123', replace(value) { opened = value; } },
    document: { getElementById: node, createElement: () => ({ textContent: '' }) },
    chrome: { storage: { local: { get: async () => ({}), set: async () => {} } } },
    fetch: async (url) => {
      if (!resolved || !url.startsWith('https://sci-hub.st/')) { return { ok: false, status: 404 }; }
      const bytes = new TextEncoder().encode('<html><iframe id="pdf" src="/article.pdf"></iframe></html>');
      let read = false;
      return { ok: true, url, headers: { get: () => 'text/html' }, body: { getReader: () => ({
        read: async () => read ? { done: true } : (read = true, { done: false, value: bytes }),
        cancel: async () => {},
      }) } };
    },
  };
  vm.runInNewContext(source, context, { filename: 'resolve.js' });
  await new Promise((resolve) => setTimeout(resolve, 40));
  if (resolved) {
    assert.match(opened, /^https:\/\/sci-hub\.st\/10\.1000\/xyz123$/);
  } else {
    assert.equal(opened, '');
    assert.match(node('status').textContent, /未确认/);
  }
}

caseRun(true).then(() => caseRun(false)).then(() => console.log('Sci-Hub DOI resolver checks passed'))
  .catch((error) => { console.error(error); process.exitCode = 1; });
