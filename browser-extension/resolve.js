const MIRRORS = [
  'https://sci-hub.se', 'https://sci-hub.st', 'https://sci-hub.ru',
  'https://www.sci-hub.se', 'https://www.sci-hub.st', 'https://www.sci-hub.ru',
];
const doi = new URLSearchParams(location.search).get('doi') || '';
const valid = /^10\.\d{4,9}\/[\w.()\-;/:]+$/i.test(doi);
const status = document.getElementById('status');
const results = document.getElementById('results');
document.getElementById('doi').textContent = valid ? doi : 'DOI 无效';
document.getElementById('official').href = valid ? `https://doi.org/${encodeURIComponent(doi)}` : 'https://doi.org/';

async function prefix(response, limit = 100000) {
  if (!response.body) { return ''; }
  const reader = response.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (length < limit) {
      const { value, done } = await reader.read();
      if (done) { break; }
      chunks.push(value); length += value.length;
    }
  } finally { await reader.cancel().catch(() => {}); }
  const bytes = new Uint8Array(Math.min(length, limit));
  let offset = 0;
  for (const chunk of chunks) {
    const part = chunk.subarray(0, bytes.length - offset);
    bytes.set(part, offset); offset += part.length;
    if (offset >= bytes.length) { break; }
  }
  return new TextDecoder().decode(bytes);
}

async function inspectMirror(mirror) {
  const path = doi.split('/').map(encodeURIComponent).join('/');
  const target = `${mirror}/${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 9000);
  try {
    const response = await fetch(target, { signal: controller.signal, redirect: 'follow', cache: 'no-store', headers: { Range: 'bytes=0-99999' } });
    if (!response.ok) { throw new Error(`HTTP ${response.status}`); }
    const finalHost = new URL(response.url).hostname;
    if (!/^(?:www\.)?sci-hub\.(?:se|st|ru)$/.test(finalHost)) { throw new Error('跳转到了其他站点'); }
    const type = response.headers.get('Content-Type') || '';
    if (/application\/pdf/i.test(type)) { return target; }
    const html = await prefix(response);
    if (html.startsWith('%PDF-')) { return target; }
    if (/captcha|cloudflare|access denied|checking your browser|article not found|paper not found/i.test(html)) {
      throw new Error('验证页或未收录');
    }
    if (/<(?:iframe|embed|object)\b[^>]*(?:id=["']pdf["']|src=["'][^"']*\.pdf|data=["'][^"']*\.pdf)/i.test(html)) {
      return target;
    }
    throw new Error('未发现论文 PDF');
  } finally { clearTimeout(timer); }
}

async function run() {
  if (!valid) { status.textContent = 'DOI 无效，无法检查。'; return; }
  const today = new Date().toLocaleDateString('sv-SE');
  const previous = (await chrome.storage.local.get('scihubProbe')).scihubProbe;
  const mirrors = previous?.date === today && MIRRORS.includes(previous.mirror)
    ? [previous.mirror, ...MIRRORS.filter((item) => item !== previous.mirror)] : MIRRORS;
  status.textContent = `正在检查 ${mirrors.length} 个镜像是否能解析这篇论文…`;
  const checks = mirrors.map(async (mirror) => {
    const row = document.createElement('li'); row.textContent = `${mirror} · 检查中`;
    results.append(row);
    try {
      const target = await inspectMirror(mirror);
      row.textContent = `${mirror} · 已发现 PDF`;
      await chrome.storage.local.set({ scihubProbe: { mirror, date: today, checkedAt: Date.now() } });
      return target;
    } catch (error) {
      row.textContent = `${mirror} · ${error.name === 'AbortError' ? '超时' : error.message || '不可用'}`;
      throw error;
    }
  });
  try {
    const target = await Promise.any(checks);
    status.textContent = '已确认镜像页面包含 PDF，正在打开…';
    location.replace(target);
  } catch (_error) {
    status.textContent = '今天未确认到可解析此 DOI 的镜像。可打开 DOI 官方页面或稍后重试。';
  }
}
run();
