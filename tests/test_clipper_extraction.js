'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('browser-extension/popup.js', 'utf8');
const start = source.indexOf('function extractVisibleArticle() {');
const end = source.indexOf('\nfunction renderPreview()', start);
assert(start >= 0 && end > start);
const extractor = source.slice(start, end);
assert(fs.existsSync('browser-extension/vendor/defuddle.js'));
assert(fs.existsSync('browser-extension/vendor/DEFUDDLE-LICENSE.txt'));

function extract({ date = '', published = '', structuredDate = '', url = 'https://example.org/news/report', selected = '', lang = 'en' }) {
  const metaNode = { getAttribute: (key) => key === 'property' ? 'article:published_time' : key === 'content' ? date : null };
  const scripts = structuredDate ? [{ textContent: JSON.stringify({ '@type': 'NewsArticle', datePublished: structuredDate }) }] : [];
  const document = {
    querySelectorAll(selector) {
      if (selector.startsWith('meta[')) { return date ? [metaNode] : []; }
      if (selector.includes('ld+json')) { return scripts; }
      return [];
    },
    querySelector(selector) { return selector === 'h1' ? { textContent: 'Example headline' } : null; },
    documentElement: { lang }, title: 'Example title',
  };
  class DOMParser {
    parseFromString(value) {
      return { body: {
        querySelectorAll: () => [],
        textContent: value.replace(/<[^>]+>/g, ''),
      } };
    }
  }
  class Defuddle {
    parse() { return { title: 'Example headline', site: 'Example', published,
      content: `<p>${'Detailed reporting on the subject. '.repeat(20)}</p>` }; }
  }
  const context = { document, DOMParser, Defuddle,
    window: { getSelection: () => ({ toString: () => selected }) }, location: new URL(url) };
  vm.runInNewContext(`${extractor}\nresult = extractVisibleArticle();`, context);
  return context.result;
}

assert.equal(extract({ date: '2026-11-09T08:45:00+08:00' }).published_at, '2026-11-09');
assert.equal(extract({ date: '2026年11月9日' }).published_at, '2026-11-09');
assert.equal(extract({ published: 'Nov 9, 2026' }).published_at, '2026-11-09');
assert.equal(extract({ structuredDate: '2026/11/09' }).published_at, '2026-11-09');
assert.equal(extract({ date: '11/09/2026', lang: 'en-US' }).published_at, '2026-11-09');
assert.equal(extract({ date: '09/11/2026', lang: 'en-GB' }).published_at, '2026-11-09');
assert.equal(extract({ date: '11/09/2026' }).published_at, '');
assert.equal(extract({ date: '2026-02-30' }).published_at, '');
assert.equal(extract({ url: 'https://example.org/2026/11/09/report' }).published_at, '2026-11-09');
assert.equal(extract({}).published_at, '');
assert.equal(extract({}).extraction_method, 'defuddle');
assert(extract({}).content.length > 300);
assert.equal(extract({ selected: 'Selected passage. '.repeat(30) }).extraction_method, 'manual_selection');
console.log('Clipper extraction metadata checks passed');
