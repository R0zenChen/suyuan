const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('browser-extension/popup.js', 'utf8');
const html = fs.readFileSync('browser-extension/popup.html', 'utf8');
const manifest = JSON.parse(fs.readFileSync('browser-extension/manifest.json', 'utf8'));
assert.ok(!source.includes('fetchPdfInPage'));
assert.ok(!html.includes('id="import-paper"'));
assert.ok(!manifest.optional_host_permissions);

const start = source.indexOf('function inspectAcademicPage()');
const end = source.indexOf('\nasync function submitPaperPdf(', start);
const inspectSource = source.slice(start, end);
const metadataNodes = [
  ['citation_title', 'Research on China'],
  ['citation_author', 'Alice Example'],
  ['citation_date', '2025-08-01'],
].map(([name, content]) => ({ content, getAttribute: (key) => key === 'name' ? name : null }));
const page = {
  title: 'Research on China',
  querySelectorAll: (selector) => selector === 'meta[name],meta[property]' ? metadataNodes : [],
  querySelector: () => null,
};
const location = new URL('https://journals.sagepub.com/doi/full/10.1177/0043820019883251');
const paper = vm.runInNewContext(`${inspectSource}\ninspectAcademicPage()`, { document: page, location, URL });
assert.equal(paper.doi, '10.1177/0043820019883251');
assert.equal(paper.metadata.authors, 'Alice Example');
assert.equal(paper.metadata.year, '2025');

const uploadStart = source.indexOf('async function uploadSelectedPaper()');
const uploadEnd = source.indexOf('\nfunction setStatus(', uploadStart);
const uploadSource = source.slice(uploadStart, uploadEnd);
let submitted;
const input = { files: [{ name: 'research.pdf', size: 13,
  arrayBuffer: async () => new TextEncoder().encode('%PDF-1.7 test').buffer }] };
const button = { disabled: false };
const context = {
  $, Uint8Array, String, btoa, Error,
  setStatus: () => {},
  submitPaperPdf: async (data, name) => { submitted = { data, name }; },
};
function $(id) { return id === 'paper-file' ? input : button; }

vm.runInNewContext(`${uploadSource}\nuploadSelectedPaper()`, context).then(() => {
  assert.equal(submitted.name, 'research.pdf');
  assert.ok(submitted.data.startsWith('JVBER'));
  assert.equal(button.disabled, false);
  console.log('Manual PDF upload checks passed');
}).catch((error) => { console.error(error); process.exitCode = 1; });
