const $ = (id) => document.getElementById(id);
const fields = ['title', 'url', 'source', 'published_at', 'language', 'content'];
let extractionMethod = 'browser_visible';
let academicPaper = null;

function inspectAcademicPage() {
  const meta = (name) => [...document.querySelectorAll('meta[name],meta[property]')]
    .find((node) => (node.getAttribute('name') || node.getAttribute('property') || '').toLowerCase() === name.toLowerCase())?.content?.trim() || '';
  const metas = (name) => [...document.querySelectorAll('meta[name],meta[property]')]
    .filter((node) => (node.getAttribute('name') || node.getAttribute('property') || '').toLowerCase() === name.toLowerCase())
    .map((node) => node.content?.trim()).filter(Boolean);
  let structured = {};
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const json = JSON.parse(script.textContent || '{}');
      const values = Array.isArray(json) ? json : [json];
      const nodes = values.flatMap((value) => Array.isArray(value['@graph']) ? value['@graph'] : [value]);
      structured = nodes.find((item) => item && /ScholarlyArticle|AcademicArticle|Thesis/i.test(String(item['@type'] || ''))) || structured;
      if (Object.keys(structured).length) { break; }
    } catch (_error) { /* Ignore malformed site metadata. */ }
  }
  const doiPattern = /\b10\.\d{4,9}\/[\w.()\-;/:]+/i;
  const doiSources = [meta('citation_doi'), meta('dc.identifier.doi'), meta('dc.identifier'), meta('prism.doi'),
    typeof structured.identifier === 'string' ? structured.identifier : structured.identifier?.value,
    document.querySelector('a[href*="doi.org/10."]')?.href,
    document.querySelector('[class*="doi" i], [id*="doi" i]')?.textContent, location.pathname];
  const doi = doiSources.map((value) => doiPattern.exec(String(value || ''))?.[0]?.replace(/[.,;)]$/, ''))
    .find(Boolean) || '';
  const authorNodes = Array.isArray(structured.author) ? structured.author : structured.author ? [structured.author] : [];
  const authors = metas('citation_author').join('; ') || metas('dc.creator').join('; ') ||
    authorNodes.map((person) => typeof person === 'string' ? person : person?.name || '').filter(Boolean).join('; ') ||
    document.querySelector('.author, .authors, #authorpart')?.textContent?.trim() || '';
  const date = meta('citation_date') || meta('citation_publication_date') || meta('dc.date') || structured.datePublished || '';
  const year = /(?:19|20)\d{2}/.exec(date)?.[0] || '';
  const title = meta('citation_title') || meta('dc.title') || structured.headline || structured.name ||
    document.querySelector('h1')?.textContent?.trim() || document.title;
  const isCnki = /(?:^|\.)cnki\.net$/i.test(location.hostname);
  const academic = Boolean(isCnki || doi || meta('citation_title') || meta('dc.title') || structured['@type']);
  if (!academic) { return null; }
  const sourceUrl = isCnki && doi ? `https://link.cnki.net/doi/${doi}` :
    isCnki ? `${location.origin}${location.pathname}` : location.href;
  return { title, doi, source_url: sourceUrl,
    metadata: { title, authors, year, doi,
      journal: meta('citation_journal_title') || meta('prism.publicationname') || '',
      publisher: meta('citation_publisher') || '',
      abstract: meta('citation_abstract') || meta('dc.description') || '' } };
}

async function submitPaperPdf(pdfBase64, filename) {
  const server = localServer();
  const token = $('token').value.trim();
  if (!server || !token) {
    $('settings').open = true;
    throw new Error('请先在连接设置中填写本机服务和配对码。');
  }
  const response = await fetch(`${server}/api/browser/paper`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Suyuan-Import-Key': token },
    body: JSON.stringify({ filename, metadata: academicPaper?.metadata || {},
      source_url: academicPaper?.source_url || '', use_vision: $('paper-vision').checked,
      pdf_base64: pdfBase64 }),
  });
  const data = await response.json();
  if (!response.ok) { throw new Error(data.error || '论文入库失败。'); }
  setStatus('已提交论文识别任务。请到研究库查看 OCR、入库和向量化进度。');
  return data;
}

async function uploadSelectedPaper() {
  const file = $('paper-file').files?.[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf') || file.size > 15000000) {
    setStatus('请选择不超过 15 MB 的 PDF 文件。', true);
    return;
  }
  const button = $('upload-paper');
  button.disabled = true;
  setStatus('正在读取所选 PDF 并提交到本机…');
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.length < 5 || String.fromCharCode(...bytes.slice(0, 5)) !== '%PDF-') {
      throw new Error('所选文件不是可读取的 PDF。');
    }
    const parts = [];
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      parts.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)));
    }
    await submitPaperPdf(btoa(parts.join('')), file.name);
  } catch (error) {
    setStatus(error.message || 'PDF 入库失败，请检查本机服务。', true);
  } finally { button.disabled = false; }
}

function setStatus(message, isError = false) {
  $('status').textContent = message;
  $('status').classList.toggle('error', isError);
}

function setConnectionResult(message, isError = false) {
  $('connection-result').textContent = message;
  $('connection-result').classList.toggle('error', isError);
}

function localServer() {
  const server = $('server').value.trim().replace(/\/$/, '');
  return /^http:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?$/.test(server) ? server : '';
}

function extractVisibleArticle() {
  const meta = (key) => [...document.querySelectorAll('meta[name], meta[property], meta[itemprop]')]
    .find((node) => (node.getAttribute('name') || node.getAttribute('property') || node.getAttribute('itemprop') || '').toLowerCase() === key.toLowerCase())
    ?.getAttribute('content')?.trim() || '';
  const cleanText = (html) => {
    const parsed = new DOMParser().parseFromString(`<html><body>${html}</body></html>`, 'text/html');
    parsed.body.querySelectorAll('script, style, nav, aside, header, footer, form, button, [role="navigation"], [role="complementary"], .share, .related, .advertisement, .newsletter, .comments')
      .forEach((node) => node.remove());
    parsed.body.querySelectorAll('br').forEach((node) => node.replaceWith(parsed.createTextNode('\n')));
    parsed.body.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, blockquote, pre, tr, section, article, div')
      .forEach((node) => node.appendChild(parsed.createTextNode('\n')));
    return (parsed.body.textContent || '').replace(/\u00a0/g, ' ').split('\n')
      .map((line) => line.replace(/[\t ]+/g, ' ').trim()).filter(Boolean).join('\n').trim();
  };
  const isoDate = (value) => {
    if (!value) { return ''; }
    const raw = String(value).trim();
    const numeric = /((?:19|20)\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})(?:日)?/.exec(raw)
      || /\b(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})\b/.exec(raw);
    let year; let month; let day;
    if (numeric) {
      if (numeric[1].length === 4) { [, year, month, day] = numeric; }
      else {
        [, day, month, year] = numeric;
        if (Number(day) <= 12 && Number(month) <= 12) {
          if (/^en-us\b/i.test(document.documentElement.lang || '')) { [day, month] = [month, day]; }
          else if (!/^(?:en-gb|en-au|en-nz|fr|de|es|pt|it)\b/i.test(document.documentElement.lang || '')) { return ''; }
        }
      }
    } else if (/(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(raw)) {
      const english = /(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?:19|20)\d{2}/i.exec(raw)?.[0];
      const parsed = new Date((english || raw).replace(/(\d)(st|nd|rd|th)\b/gi, '$1'));
      if (Number.isNaN(parsed.getTime())) { return ''; }
      year = parsed.getFullYear(); month = parsed.getMonth() + 1; day = parsed.getDate();
    } else { return ''; }
    const valid = new Date(Number(year), Number(month) - 1, Number(day));
    if (valid.getFullYear() !== Number(year) || valid.getMonth() + 1 !== Number(month) || valid.getDate() !== Number(day)) { return ''; }
    return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  };
  const selected = window.getSelection()?.toString().trim() || '';
  let result = null;
  try { if (typeof Defuddle === 'function') { result = new Defuddle(document, { url: location.href }).parse(); } }
  catch (_error) { /* A site-specific DOM can defeat Defuddle; use the cleaned DOM fallback. */ }
  const structuredBodies = [];
  const dateCandidates = [
    meta('article:published_time'), meta('citation_date'), meta('citation_publication_date'),
    meta('datePublished'), meta('pubdate'), meta('date'), meta('dc.date.issued'),
    meta('dc.date'), meta('parsely-pub-date'), meta('sailthru.date'),
    meta('prism.publicationdate'), meta('article.published'),
  ];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const walk = (node, depth = 0) => {
        if (!node || typeof node !== 'object' || depth > 5) { return; }
        if (node.datePublished) { dateCandidates.push(node.datePublished); }
        if (typeof node.articleBody === 'string') { structuredBodies.push(node.articleBody); }
        for (const child of Object.values(node)) { if (typeof child === 'object') { walk(child, depth + 1); } }
      };
      walk(JSON.parse(script.textContent || '{}'));
    } catch (_error) { /* Ignore malformed JSON-LD. */ }
  }
  dateCandidates.push(result?.published);
  const timeNodes = document.querySelectorAll('time[datetime], time[dateTime], [itemprop="datePublished"], [class*="publish-date" i], [class*="publish-time" i], [class*="article-date" i], [class*="timestamp" i]');
  for (const node of [...timeNodes].slice(0, 25)) {
    dateCandidates.push(node.getAttribute('datetime') || node.getAttribute('content') || node.textContent);
  }
  dateCandidates.push(location.pathname);
  const publishedAt = dateCandidates.map(isoDate).find(Boolean) || '';
  let content = '';
  let method = 'defuddle';
  if (selected.length >= 300) { content = selected; method = 'manual_selection'; }
  else if (result?.content) { content = cleanText(result.content); }
  if (content.length < 300 && method !== 'manual_selection') {
    const candidates = [...document.querySelectorAll('article, [itemprop="articleBody"], [class*="article-body" i], [class*="article-content" i], [class*="entry-content" i], main')];
    const scored = candidates.map((node) => {
      const clone = node.cloneNode(true);
      clone.querySelectorAll('nav, aside, header, footer, form, button, script, style, [role="navigation"], [role="complementary"], [class*="share" i], [class*="related" i], [class*="sidebar" i], [class*="comment" i], [class*="advert" i], [class*="promo" i]')
        .forEach((item) => item.remove());
      const text = cleanText(clone.outerHTML);
      const linkLength = [...clone.querySelectorAll('a')].reduce((sum, link) => sum + (link.textContent || '').length, 0);
      return { text, score: text.length - linkLength * 2 + clone.querySelectorAll('p').length * 80 };
    }).filter((item) => item.text.length >= 300).sort((a, b) => b.score - a.score);
    if (scored.length) { content = scored[0].text; method = 'clean_dom'; }
  }
  if (content.length < 300 && method !== 'manual_selection') {
    const structuredBody = structuredBodies.sort((a, b) => b.length - a.length)[0] || result?.schemaOrgData?.articleBody;
    if (typeof structuredBody === 'string') { content = cleanText(structuredBody); method = 'page_structured_data'; }
  }
  const lang = (result?.language || document.documentElement.lang || 'en').toLowerCase().split('-')[0];
  return {
    title: result?.title || meta('og:title') || document.querySelector('h1')?.textContent?.trim() || document.title,
    url: document.querySelector('link[rel="canonical"]')?.href || location.href,
    source: result?.site || meta('og:site_name') || location.hostname,
    published_at: publishedAt,
    language: lang,
    content: content.slice(0, 80000),
    extraction_method: method,
  };
}

function renderPreview() {
  const title = $('title').value.trim();
  const content = $('content').value.trim();
  const date = $('published_at').value;
  $('preview-title').textContent = title || '未识别文章标题';
  $('preview-meta').replaceChildren();
  for (const value of [$('source').value.trim(), date || '发布日期未识别', $('language').selectedOptions[0]?.textContent]) {
    if (!value) { continue; }
    const span = document.createElement('span');
    span.textContent = value;
    $('preview-meta').append(span);
  }
  $('length').textContent = `${content.length.toLocaleString()} 字`;
  $('preview-text').textContent = content.slice(0, 360) || '未提取到正文。请确认页面已加载，或展开下方手动粘贴。';
  const enough = content.length >= 300;
  $('capture').classList.toggle('warn', !enough);
  $('capture-label').textContent = enough
    ? (extractionMethod === 'manual_selection' ? '已提取选中的内容' : '已清理页面正文')
    : '正文不足，需补充';
  $('hint').textContent = !enough ? '正文少于 300 字。请等待页面加载、重新读取，或展开下方补充。'
    : !date ? '未识别到发布日期；可以先保存，之后再核对补充。'
      : '已自动识别发布日期；保存前可检查正文和日期。';
  $('import').disabled = !title || !$('url').value.trim() || !enough;
}

async function readPage() {
  setStatus('正在读取当前页面…');
  academicPaper = null;
  $('paper-title').textContent = '选择 PDF 文件入库';
  $('paper-note').textContent = '先从论文网站下载 PDF，再在下方选择文件。';
  $('paper-doi').hidden = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https?:\/\//.test(tab.url || '')) { throw new Error('当前页面无法读取；仍可选择本机 PDF 入库。'); }
    const activeUrl = new URL(tab.url);
    const isPdfTab = activeUrl.protocol === 'https:' && /\.pdf(?:$|[?#])|\/pdf(?:\/|$)/i.test(tab.url);
    if (activeUrl.protocol === 'https:') {
      if (isPdfTab) {
        const title = String(tab.title || activeUrl.pathname.split('/').pop() || 'paper.pdf').replace(/\.pdf\b.*/i, '').trim();
        academicPaper = { title, source_url: tab.url, doi: '', metadata: { title } };
      } else {
        try {
          const detected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: inspectAcademicPage });
          academicPaper = detected?.[0]?.result || null;
        } catch (_error) { /* PDF import from file remains available. */ }
      }
      if (academicPaper) {
        $('paper-title').textContent = academicPaper.title;
        $('paper-note').textContent = '已识别当前页面的论文信息；选择对应 PDF 后会一起入库。';
        $('paper-doi').hidden = !academicPaper.doi;
        $('paper-doi-text').textContent = academicPaper.doi || '';
        if (academicPaper.doi) { $('paper-scihub').href = `https://doi.org/${encodeURIComponent(academicPaper.doi)}`; }
      }
    }
    if (isPdfTab) {
      $('preview-title').textContent = academicPaper.title;
      $('capture-label').textContent = 'PDF 页面';
      $('preview-text').textContent = '请先把 PDF 保存到本机，再用上方入口导入；网页正文剪藏不适用于 PDF 预览器。';
      $('import').disabled = true;
      setStatus('');
      return;
    }
    $('import').textContent = academicPaper ? '仅保存网页正文（不含 PDF）' : '保存到溯源';
    try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['vendor/defuddle.js'] }); }
    catch (_error) { /* The cleaned DOM fallback remains available. */ }
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractVisibleArticle });
    const article = results?.[0]?.result;
    if (!article) { throw new Error('页面没有返回可读内容。'); }
    for (const field of fields) {
      if (field !== 'language') { $(field).value = article[field] || ''; }
    }
    $('language').value = [...$('language').options].some((option) => option.value === article.language) ? article.language : 'en';
    extractionMethod = article.extraction_method || 'browser_visible';
    renderPreview();
    if ($('token').value.trim()) { setStatus(''); }
  } catch (error) {
    $('preview-title').textContent = '无法读取当前页面';
    $('capture-label').textContent = '等待正文';
    $('import').disabled = true;
    $('review').open = true;
    setStatus(error.message || '读取失败。可以展开下方手动填写。', true);
  }
}

async function importPage() {
  const server = localServer();
  if (!server) {
    $('settings').open = true;
    setStatus('请在连接设置中填写本机溯源服务地址。', true);
    return;
  }
  const token = $('token').value.trim();
  if (!token) {
    $('settings').open = true;
    setStatus('首次使用：请粘贴溯源网页显示的配对码。', true);
    $('token').focus();
    return;
  }
  const payload = Object.fromEntries(fields.map((field) => [field, $(field).value.trim()]));
  payload.extraction_method = extractionMethod;
  $('import').disabled = true;
  $('import').textContent = '正在保存…';
  setStatus('');
  try {
    const response = await fetch(`${server}/api/browser/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Suyuan-Import-Key': token },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '保存失败。'); }
    await chrome.storage.local.set({ server, token });
    $('open').href = server;
    $('settings').open = false;
    setConnectionResult('连接正常');
    $('import').textContent = '已保存 · 点击可更新';
    setStatus(data.article?.stored_in_sql
      ? '已入 SQL 新闻库。下一轮研判前，可在网页导入列表中勾选这篇文章。'
      : (data.warning || '文章已保存到本机，待 SQL 连接恢复后入库。'));
  } catch (error) {
    $('import').textContent = '保存到溯源';
    $('settings').open = true;
    setStatus(error.message || '无法连接本机溯源，请检查服务是否正在运行。', true);
  } finally {
    renderPreview();
  }
}

async function testConnection() {
  const server = localServer();
  if (!server) {
    setConnectionResult('请填写本机服务地址。', true);
    return;
  }
  const token = $('token').value.trim();
  if (!token) {
    setConnectionResult('请先粘贴配对码。', true);
    $('token').focus();
    return;
  }
  const button = $('test-connection');
  button.disabled = true;
  setConnectionResult('正在测试…');
  try {
    const response = await fetch(`${server}/api/browser/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Suyuan-Import-Key': token },
      body: '{}',
      signal: AbortSignal.timeout(5000),
    });
    let data;
    try { data = await response.json(); }
    catch (_error) { throw new Error('该地址没有返回溯源服务响应。'); }
    if (!response.ok) { throw new Error(data.error || '连接失败，请检查配对码。'); }
    if (!data.ok || data.service !== '溯源') { throw new Error('该地址不是溯源服务。'); }
    await chrome.storage.local.set({ server, token });
    $('open').href = server;
    setConnectionResult(data.sql_connected ? '连接成功，SQL 已就绪。' : '连接成功；SQL 尚未就绪，文章会先保存本机。');
  } catch (error) {
    setConnectionResult(error.name === 'TimeoutError' ? '连接超时，请检查服务。'
      : error instanceof TypeError ? '无法连接本机服务，请先启动溯源。'
        : error.message || '测试失败。', true);
  } finally {
    button.disabled = false;
  }
}

$('import').addEventListener('click', importPage);
$('upload-paper').addEventListener('click', uploadSelectedPaper);
$('paper-file').addEventListener('change', () => { $('upload-paper').disabled = !$('paper-file').files?.length; });
$('read').addEventListener('click', readPage);
$('test-connection').addEventListener('click', testConnection);
for (const field of fields) { $(field).addEventListener('input', renderPreview); }
$('content').addEventListener('input', () => { extractionMethod = 'manual_selection'; renderPreview(); });
$('server').addEventListener('input', () => {
  setConnectionResult('');
  const server = $('server').value.trim();
  if (/^http:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?\/?$/.test(server)) { $('open').href = server; }
});
$('token').addEventListener('input', () => setConnectionResult(''));

$('paper-scihub').addEventListener('click', (event) => {
  if (!academicPaper?.doi) { return; }
  event.preventDefault();
  chrome.runtime.sendMessage({ type: 'openSciHubResolver', doi: academicPaper.doi }, () => {
    if (chrome.runtime.lastError) { chrome.tabs.create({ url: $('paper-scihub').href }); }
  });
});
chrome.storage.local.get(['server', 'token']).then((saved) => {
  if (saved.server) { $('server').value = saved.server; $('open').href = saved.server; }
  if (saved.token) { $('token').value = saved.token; setStatus(''); }
  else {
    $('settings').open = true;
    setStatus('首次使用：粘贴一次配对码，以后直接点“保存到溯源”。');
    requestAnimationFrame(() => $('settings').scrollIntoView({ block: 'nearest' }));
  }
});
readPage();
