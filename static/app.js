'use strict';

const form = document.querySelector('#research-form');
const taskInput = document.querySelector('#task');
const runButton = document.querySelector('#run-button');
const state = document.querySelector('#system-state');
const message = document.querySelector('#message');
const results = document.querySelector('#results');
const archiveList = document.querySelector('#archive-list');
const archiveState = document.querySelector('#archive-state');
const archiveSearch = document.querySelector('#archive-search');
const archiveMatchCount = document.querySelector('#archive-match-count');
const archivePrev = document.querySelector('#archive-prev');
const archiveNext = document.querySelector('#archive-next');
const coveragePanel = document.querySelector('#coverage');
const jobProgress = document.querySelector('#job-progress');
const browserImportList = document.querySelector('#browser-import-list');
let savedRuns = [];
let savedWatches = [];
let selectedRunId = null;
let activeRun = null;
let candidateVisibleLimit = 80;
let evidenceReviews = new Map();

function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[character]));
}

function showMessage(text, type = 'error') {
  message.textContent = text;
  message.className = `message ${type}`;
  message.hidden = false;
}

function hideMessage() {
  message.hidden = true;
}

async function loadSearchLanguages() {
  try {
    const response = await fetch('/api/search/languages', {cache:'no-store'});
    if (!response.ok) { return; }
    const data = await response.json();
    const names = (data.default || []).map((item) => item.name);
    if (!names.length) { return; }
    document.querySelector('#language-defaults').textContent = `默认检索${names.join('、')}，共${names.length * data.queries_per_language}组检索式，最多四路并行；可填写自定义语种缩小范围（最多${data.max_languages}种）。留空日期则查近40天，每组最多返回50条，触顶时按日期拆分。候选逐条取文、去重入库，再选30–50篇独立长篇代表材料归纳观点和至少4个风险点。`;
    document.querySelector('#languages').placeholder = `留空用${names.length}语种；或填：俄语,阿拉伯语,葡语`;
  } catch (error) { /* 页面中的默认语种说明仍可使用。 */ }
}

function labelForLevel(level) {
  return level || '仅索引线索';
}

function externalUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch (error) {
    return '';
  }
}

function readableDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? (value || '时间未知') : date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
}

function renderInsights(assessment) {
  const groups = [
    ['传播点', assessment.transmission_points || []],
    ['叙事', assessment.narratives || []],
  ];
  const output = groups.flatMap(([type, entries]) => entries.map((entry) => `
    <article class="insight">
      <div class="insight-head"><span class="insight-type">${type.toUpperCase()}</span><h4>${escapeHtml(entry.label)}</h4></div>
      <p>${escapeHtml(entry.why || entry.stance || '待补充说明')} <span class="evidence-id">${escapeHtml((entry.evidence_ids || []).join(' · '))}</span></p>
    </article>
  `)).join('');
  return output || '<p class="risk-reason">尚未形成带充分证据编号的传播点或叙事判断。</p>';
}

function renderSources(articles) {
  const counts = {};
  articles.forEach((article) => { counts[labelForLevel(article.evidence_level)] = (counts[labelForLevel(article.evidence_level)] || 0) + 1; });
  document.querySelector('#source-count').textContent = `· ${articles.length} 条`;
  document.querySelector('#evidence-ribbon').innerHTML = Object.entries(counts).map(([level, count]) => (
    `<span class="level-${escapeHtml(level)}" style="flex:${count}" title="${escapeHtml(level)}：${count}条"></span>`
  )).join('');
  document.querySelector('#source-list').innerHTML = articles.map((article) => {
    const newsUrl = externalUrl(article.url);
    const archiveUrl = externalUrl(article.archive && article.archive.snapshot_url);
    const excerpt = article.excerpt || '未提供摘要。';
    const preview = excerpt.length > 220 ? `${excerpt.slice(0, 220)}…` : excerpt;
    const more = excerpt.length > 220
      ? `<details class="source-excerpt"><summary>展开完整摘要</summary><p>${escapeHtml(excerpt)}</p></details>` : '';
    const title = newsUrl
      ? `<a href="${escapeHtml(newsUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a>`
      : escapeHtml(article.title);
    const archive = archiveUrl
      ? `<a class="archive-link" href="${escapeHtml(archiveUrl)}" target="_blank" rel="noopener noreferrer">查看历史快照线索 ↗</a>` : '';
    const sample = article.sample_status
      ? `<span class="sample-status">${escapeHtml(article.search_language || '')} 检索 · ${escapeHtml(article.genre || article.sample_status)}</span>` : '';
    const contentUrl = externalUrl(article.content_url);
    const contentLink = contentUrl && contentUrl !== newsUrl
      ? `<a class="archive-link" href="${escapeHtml(contentUrl)}" target="_blank" rel="noopener noreferrer">查看实际取文版本 ↗</a>` : '';
    return `
      <article id="source-${escapeHtml(article.id)}" class="source ${escapeHtml(labelForLevel(article.evidence_level))}">
        <div class="source-top"><span>${escapeHtml(article.id)} · ${escapeHtml(article.source)}${article.published_at ? ` · ${escapeHtml(readableDate(article.published_at))}` : ''}</span><span class="badge">${escapeHtml(labelForLevel(article.evidence_level))}</span></div>
        <h4>${title}</h4>
        <p>${escapeHtml(preview)}</p>${more}
        <p class="source-note">${escapeHtml(article.access_note)}</p>${sample}${contentLink}${archive}
      </article>`;
  }).join('') || '<p>没有获得候选来源。</p>';
}

function renderCoverage(plan) {
  const coverage = plan.coverage;
  coveragePanel.hidden = !coverage;
  if (!coverage) { return; }
  document.querySelector('#coverage-status').textContent = coverage.status;
  document.querySelector('#coverage-count').textContent = `去重候选 ${coverage.candidate_count ?? coverage.fetched_count} · 已取文 ${coverage.fetched_count} · 合格 ${coverage.eligible_count ?? coverage.initial_pass} · 代表材料 ${coverage.initial_pass}/${coverage.target}–${coverage.selected_max ?? 50} · 风险点 ${coverage.risk_points ?? 0}/${coverage.required_risk_points ?? 4}`;
  const fallback = plan.query_fallback_languages || [];
  document.querySelector('#coverage-note').textContent = `触及单次50条上限时按日期拆分，共调用 ${coverage.search_calls ?? '—'} 次检索；${coverage.saturated_days?.length ? `${coverage.saturated_days.length} 个单日仍触顶，结果可能未穷尽。` : '未发现单日触顶。'}同稿 ${coverage.duplicate_count ?? 0}，排除 ${coverage.excluded_count ?? 0}。${plan.new_count !== undefined ? `本次新增代表材料 ${plan.new_count} 篇。` : ''}作者、首发和快照来源仍需逐篇核验。${fallback.length ? `以下语种的检索式使用降级生成：${fallback.join('、')}。` : ''}`;
  document.querySelector('#language-coverage').innerHTML = (coverage.languages || []).map((item) => `
    <div class="language-item"><strong>${escapeHtml(item.language.toUpperCase())}</strong><span>${item.queries} 组检索 / ${item.search_calls ?? item.queries} 次调用 · ${item.candidates} 候选 · ${item.fetched} 取文 · ${item.initial_pass} 入选${item.saturated_days?.length ? ` · ${item.saturated_days.length} 天触顶` : ''}${item.errors ? ` · ${item.errors} 次失败` : ''}</span></div>
  `).join('');
  document.querySelector('#query-list').innerHTML = (plan.query_specs || []).map((item) => `<p><b>${escapeHtml(item.language.toUpperCase())}</b> ${escapeHtml(item.query)}</p>`).join('');
}

function renderArchiveHints(plan) {
  const hints = plan.archive_hints || [];
  const panel = document.querySelector('#archive-hints');
  panel.hidden = hints.length === 0;
  document.querySelector('#archive-hint-count').textContent = `· ${hints.length} 条`;
  document.querySelector('#archive-hint-list').innerHTML = hints.map((item) => {
    const original = externalUrl(item.url);
    const snapshot = externalUrl(item.archive && item.archive.snapshot_url);
    return `<article class="archive-hint"><strong>${escapeHtml(item.source || '来源待核')} · ${escapeHtml(item.title)}</strong><p>${escapeHtml(item.access_note || '')}</p><div>${original ? `<a href="${escapeHtml(original)}" target="_blank" rel="noopener noreferrer">原始页面 ↗</a>` : ''}${snapshot ? `<a href="${escapeHtml(snapshot)}" target="_blank" rel="noopener noreferrer">公开快照 ↗</a>` : ''}</div></article>`;
  }).join('');
}

function renderRiskSections(assessment, articles, show, validated = false) {
  const panel = document.querySelector('#risk-sections');
  panel.hidden = !show;
  if (!show) { return; }
  const points = assessment.risk_points || [];
  const byId = new Map(articles.map((article) => [article.id, article]));
  const passages = new Map(articles.flatMap((article) => (article.passages || []).map((passage) => [passage.id, { ...passage, article }])));
  document.querySelector('#risk-sections-state').textContent = validated
    ? `${points.length}/4 个风险点达到每点4篇报道`
    : '旧版风险点，尚未按独立长篇和同稿去重标准复核';
  document.querySelector('#risk-section-list').innerHTML = points.map((point, index) => `
    <article class="risk-section">
      <h3>${index + 1}. ${escapeHtml(point.heading)}</h3>
      <p>${escapeHtml(point.analysis || '请结合下列报道人工复核。')}</p>
      ${point.translation_note ? `<p class="translation-note">跨语种表述：${escapeHtml(point.translation_note)}</p>` : ''}
      ${point.counter_evidence_ids?.length ? `<p class="counter-note">反向材料：${point.counter_evidence_ids.map((id) => `<a href="#source-${escapeHtml(id)}">${escapeHtml(id)}</a>`).join(' · ')}（模型初判，需核对）</p>` : ''}
      ${point.counter_passage_ids?.length ? `<details class="risk-quotes"><summary>核对反向原文段落 · ${point.counter_passage_ids.length} 条</summary>${point.counter_passage_ids.map((id) => {
        const passage = passages.get(id);
        return passage ? `<blockquote><strong>${escapeHtml(id)} · ${escapeHtml(passage.article.source || '')}</strong><p>${escapeHtml(passage.text)}</p></blockquote>` : '';
      }).join('')}</details>` : ''}
      ${point.evidence_passage_ids?.some((id) => ['disputed', 'excluded'].includes(evidenceReviews.get(id)?.status)) ? '<p class="review-warning">部分支撑段落已被标记为有争议或不采用，本风险判断需要重新核验。</p>' : ''}
      ${point.evidence_strength ? `<div class="risk-score"><strong>证据强度 ${point.evidence_strength.score}/100</strong><span>${Object.entries(point.evidence_strength.components || {}).map(([name, value]) => `${escapeHtml(name)} ${value}`).join(' · ')}</span></div>` : ''}
      ${point.risk_score ? `<p class="risk-score-note">模型风险估计 ${point.risk_score}/25（发生倾向 ${point.likelihood}/5 × 影响程度 ${point.impact}/5）。${escapeHtml(point.score_reason || '')}</p>` : ''}
      <div class="risk-evidence"><strong>支撑报道 · ${point.evidence_ids.length} 篇</strong><ol>${point.evidence_ids.map((id) => {
        const article = byId.get(id);
        return article ? `<li><a href="#source-${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(article.title)}</a></li>` : '';
      }).join('')}</ol></div>
      ${(point.evidence_passage_ids || []).length ? `<details class="risk-quotes"><summary>核对原文段落 · ${point.evidence_passage_ids.length} 条</summary>${point.evidence_passage_ids.map((id) => {
        const passage = passages.get(id);
        if (!passage) { return ''; }
        const link = externalUrl(passage.article.content_url || passage.article.url);
        const review = evidenceReviews.get(id);
        const reviewLabel = review?.stale ? '核验已过期' : ({verified:'已核实',disputed:'有争议',excluded:'不采用'}[review?.status] || '待核验');
        return `<blockquote><strong>${escapeHtml(id)} · ${escapeHtml(passage.article.source || '')} · ${reviewLabel}</strong><p>${escapeHtml(passage.text)}</p>${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">查看实际取文页面 ↗</a>` : ''}</blockquote>`;
      }).join('')}</details>` : ''}
    </article>
  `).join('') || '<p class="archive-empty">尚未形成至少4篇不同报道支撑的风险点，本次研判仍待补充材料。</p>';
}

function renderViewpoints(assessment, articles) {
  const panel = document.querySelector('#viewpoints');
  const points = assessment.viewpoints || [];
  panel.hidden = points.length === 0;
  const byId = new Map(articles.map((article) => [article.id, article]));
  document.querySelector('#viewpoint-list').innerHTML = points.map((point, index) => `
    <article class="viewpoint"><span>观点 ${String(index + 1).padStart(2, '0')}</span><h4>${escapeHtml(point.claim)}</h4><p>${escapeHtml(point.difference || '')}</p><div>${(point.evidence_ids || []).map((id) => byId.has(id) ? `<a href="#source-${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(byId.get(id).source || '')}</a>` : '').join('')}</div></article>
  `).join('');
}

function renderTrendBars(selector, counts, limit = 8) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, limit);
  const max = Math.max(1, ...entries.map((item) => item[1]));
  document.querySelector(selector).innerHTML = entries.map(([label, count]) => `
    <div class="trend-row"><span title="${escapeHtml(label)}">${escapeHtml(label)}</span><div><i style="width:${Math.max(3, Math.round(count / max * 100))}%"></i></div><strong>${count}</strong></div>
  `).join('') || '<p class="trend-empty">暂无可比较的日期或来源。</p>';
}

function renderTrends(run) {
  const seen = new Set();
  const candidates = (run.candidates?.length ? run.candidates : run.articles).filter((article) => {
    const key = article.global_article_id || article.url;
    if (seen.has(key)) { return false; }
    seen.add(key);
    return true;
  });
  const dates = {}, languages = {}, sources = {};
  candidates.forEach((article) => {
    const day = String(article.published_at || '').slice(0, 10);
    if (/^20\d{2}-\d{2}-\d{2}$/.test(day)) { dates[day] = (dates[day] || 0) + 1; }
    const language = article.search_language || '未标注';
    languages[language] = (languages[language] || 0) + 1;
    const source = article.source || '未标注';
    sources[source] = (sources[source] || 0) + 1;
  });
  const orderedDates = Object.entries(dates).sort((a, b) => a[0].localeCompare(b[0]));
  const maxDate = Math.max(1, ...orderedDates.map((item) => item[1]));
  document.querySelector('#trend-dates').innerHTML = orderedDates.map(([day, count]) => `<div class="trend-day" title="${day}：${count} 篇"><span>${count}</span><i style="height:${Math.max(7, Math.round(count / maxDate * 100))}%"></i><small>${day.slice(5)}</small></div>`).join('') || '<p class="trend-empty">候选来源缺少可用发布日期。</p>';
  renderTrendBars('#trend-languages', languages, 12);
  renderTrendBars('#trend-sources', sources, 8);
  const watch = savedWatches.find((item) => item.id === run.plan.watch_id || item.history?.some((entry) => entry.run_id === run.id));
  const history = document.querySelector('#trend-history');
  history.hidden = !watch || !watch.history?.length;
  if (!history.hidden) {
    const top = (counts = {}) => Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] || '—';
    const entries = watch.history.slice(-12);
    const latest = entries.at(-1);
    const previous = entries.at(-2);
    history.innerHTML = `<h4>跨次对比 · ${escapeHtml(watch.topic)}</h4><div class="trend-history-bars">${entries.map((entry) => `<div title="${escapeHtml(readableDate(entry.created_at))}：新增 ${entry.new_count} 篇，候选新风险点 ${entry.new_risk_count} 个"><strong>${entry.new_count}</strong><i style="height:${Math.max(8, Math.min(100, entry.new_count * 10))}%"></i><small>${escapeHtml(String(entry.created_at).slice(5, 10))}</small></div>`).join('')}</div><div class="trend-table-wrap"><table class="trend-table"><thead><tr><th>运行</th><th>去重候选</th><th>新增代表</th><th>风险点</th><th>主要检索语种</th><th>主要来源</th></tr></thead><tbody>${entries.map((entry) => `<tr><td>${escapeHtml(String(entry.created_at || '').slice(0, 10))} ${entry.kind === 'baseline' ? '基线' : '增量'}</td><td>${entry.candidate_count ?? '—'}</td><td>${entry.new_count}</td><td>${entry.risk_count}</td><td>${escapeHtml(top(entry.language_counts))}</td><td>${escapeHtml(top(entry.source_counts))}</td></tr>`).join('')}</tbody></table></div>${latest?.risk_point_counts?.length ? `<div class="trend-risk-points"><strong>最新风险点 · 引用篇数变化</strong>${latest.risk_point_counts.map((point) => { const old = previous?.risk_point_counts?.find((item) => item.heading === point.heading); return `<p>${escapeHtml(point.heading)} <b>${point.articles}${old ? ` / 上次 ${old.articles}` : ' / 新出现'}</b></p>`; }).join('')}</div>` : ''}<p>仅比较系统采集样本；新增风险点为自动判断，须人工复核。</p>`;
  }
}

function renderCandidates(run) {
  const panel = document.querySelector('#candidate-section');
  const candidates = run.candidates || [];
  panel.hidden = candidates.length === 0 || run.plan.selection_policy !== 'representative_v2';
  if (panel.hidden) { return; }
  document.querySelector('#candidate-count').textContent = `· ${candidates.length} 条`;
  const query = document.querySelector('#candidate-search').value.trim().toLocaleLowerCase();
  const filtered = query ? candidates.filter((article) => [article.title, article.source, article.sample_status].join(' ').toLocaleLowerCase().includes(query)) : candidates;
  document.querySelector('#candidate-match').textContent = `${filtered.length} / ${candidates.length}`;
  document.querySelector('#candidate-list').innerHTML = filtered.slice(0, candidateVisibleLimit).map((article) => {
    const link = externalUrl(article.url);
    const canImport = link && !['公开正文', '公开存档正文', '公开转载正文', '浏览器导入正文'].includes(article.evidence_level);
    return `<article class="candidate"><span>${escapeHtml(article.id)} · ${escapeHtml(article.source || '')} · ${escapeHtml(article.search_language || '')}</span><strong>${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a>` : escapeHtml(article.title)}</strong><em>${escapeHtml(article.sample_status || '待核验')}${article.selection_rank ? ` · 代表材料第 ${article.selection_rank} 位` : ''}</em>${canImport ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">打开原站，用浏览器扩展补全文 ↗</a>` : ''}</article>`;
  }).join('') || '<p class="archive-empty">没有匹配的候选。</p>';
  document.querySelector('#candidate-more').hidden = filtered.length <= candidateVisibleLimit;
}

function renderClaimTimeline(run) {
  const panel = document.querySelector('#claim-timeline');
  const watch = savedWatches.find((item) => item.id === run.plan.watch_id || item.history?.some((entry) => entry.run_id === run.id));
  const claims = (watch?.claims || []).filter((claim) => claim.history?.some((entry) => entry.run_id === run.id));
  panel.hidden = claims.length === 0;
  if (panel.hidden) { return; }
  document.querySelector('#claim-timeline-list').innerHTML = claims.map((claim) => {
    const index = claim.history.findIndex((entry) => entry.run_id === run.id);
    const current = claim.history[index];
    const previous = index > 0 ? claim.history[index - 1] : null;
    const oldUrls = new Set(previous?.support_urls || []);
    const added = (current.support_urls || []).filter((url) => !oldUrls.has(url)).length;
    const delta = Number.isInteger(current.risk_score) && Number.isInteger(previous?.risk_score)
      ? current.risk_score - previous.risk_score : null;
    const change = current.status === 'not_repeated' ? '本轮未重现；不能据此判断主张已经消失'
      : previous ? `新增支撑来源 ${added} 个 · 风险估计 ${delta === null ? '待核' : delta === 0 ? '持平' : `${delta > 0 ? '+' : ''}${delta} / 25`}` : '本次首次记录该主张';
    const first = claim.history.slice(0, index + 1).map((entry) => entry.first_observed)
      .filter(Boolean).sort((a, b) => String(a.date || '9999').localeCompare(String(b.date || '9999')))[0];
    const firstLink = externalUrl(first?.url);
    return `<article class="claim-card"><div><span class="claim-id">主张 ${escapeHtml(claim.id)}</span><h4>${escapeHtml(current.heading)}</h4><strong>${escapeHtml(change)}</strong></div>
      <p>本库最早材料：${first ? `${escapeHtml(first.date || '日期待核')} · ${escapeHtml(first.source || '来源待核')}${firstLink ? ` · <a href="${escapeHtml(firstLink)}" target="_blank" rel="noopener noreferrer">原文 ↗</a>` : ''}` : '尚无可核材料'}</p>
      <p>支撑 ${current.support_ids.length} 篇 · 反向 ${current.counter_ids.length} 篇 · 涉及语种 ${escapeHtml(current.languages.join('、') || '待核')}</p>
      <p>支撑材料来源：${escapeHtml((current.support_sources || []).join('、') || '待核')}；反向材料来源：${escapeHtml((current.counter_sources || []).join('、') || '本轮未检索到')}</p>
      ${current.score_reason ? `<p>本轮风险判断依据：${escapeHtml(current.score_reason)}</p>` : ''}
      ${current.translation_note ? `<p>译法／语义变化（模型提示）：${escapeHtml(current.translation_note)}</p>` : ''}
      ${current.original_quotes.length ? `<details><summary>对照各语种原文摘录</summary>${current.original_quotes.map((quote) => `<blockquote><b>${escapeHtml(quote.language)} · ${escapeHtml(quote.source || '')}</b><p>${escapeHtml(quote.text)}</p>${externalUrl(quote.url) ? `<a href="${escapeHtml(externalUrl(quote.url))}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>` : ''}</blockquote>`).join('')}</details>` : ''}
      <small>自动关联的相似主张及最早材料需人工核验；不能据此断定全网首发。</small></article>`;
  }).join('');
}

function renderEvidenceWorkbench(run) {
  const panel = document.querySelector('#review-workbench');
  const used = new Set((run.assessment.risk_points || []).flatMap((point) =>
    [...(point.evidence_passage_ids || []), ...(point.counter_passage_ids || [])]));
  const passages = run.articles.flatMap((article) => (article.passages || [])
    .filter((passage) => used.has(passage.id)).map((passage) => ({...passage, article})));
  panel.hidden = passages.length === 0;
  if (panel.hidden) { return; }
  const verified = passages.filter((item) => evidenceReviews.get(item.id)?.status === 'verified' && !evidenceReviews.get(item.id)?.stale).length;
  const disputed = passages.filter((item) => ['disputed','excluded'].includes(evidenceReviews.get(item.id)?.status)).length;
  document.querySelector('#review-summary').textContent = `${passages.length} 条支撑段落 · 已核实 ${verified} · 有争议／不采用 ${disputed}。核验状态不会自动修改模型分数。`;
  document.querySelector('#review-list').innerHTML = passages.map(({id, text, article}) => {
    const review = evidenceReviews.get(id);
    const url = externalUrl(article.content_url || article.url);
    return `<article class="review-item" data-passage-id="${escapeHtml(id)}"><div class="review-head"><strong>${escapeHtml(id)} · ${escapeHtml(article.source || '来源待核')}</strong><span>${escapeHtml(article.published_at || '日期待核')} · ${escapeHtml(article.search_language || '语种待核')} · ${escapeHtml(article.evidence_level || '')}</span></div>
      <p>${escapeHtml(text)}</p><div class="review-links">${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">实际取文版本 ↗</a>` : ''}${article.url !== article.content_url && externalUrl(article.url) ? `<a href="${escapeHtml(externalUrl(article.url))}" target="_blank" rel="noopener noreferrer">原始报道 ↗</a>` : ''}</div>
      <div class="review-actions"><select aria-label="${escapeHtml(id)} 核验结果"><option value="pending" ${!review || review.status === 'pending' ? 'selected' : ''}>待核验</option><option value="verified" ${review?.status === 'verified' ? 'selected' : ''}>已核实</option><option value="disputed" ${review?.status === 'disputed' ? 'selected' : ''}>有争议</option><option value="excluded" ${review?.status === 'excluded' ? 'selected' : ''}>不采用</option></select>
      <input aria-label="${escapeHtml(id)} 核验备注" maxlength="1000" placeholder="核对结果、日期或译法备注" value="${escapeHtml(review?.note || '')}"><button type="button" data-save-review="${escapeHtml(id)}">保存核验</button></div>
      ${review?.stale ? '<small class="review-warning">原文段落已变化，此核验需重做。</small>' : ''}</article>`;
  }).join('');
}

async function loadEvidenceReviews(run) {
  evidenceReviews = new Map();
  try {
    const response = await fetch(`/api/evidence-reviews/${run.id}`, {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '读取核验状态失败'); }
    if (selectedRunId !== run.id) { return; }
    evidenceReviews = new Map((data.reviews || []).map((review) => [review.passage_id, review]));
    renderEvidenceWorkbench(run);
    renderRiskSections(run.assessment, run.articles, !document.querySelector('#risk-sections').hidden,
      ['longform_original_v1','representative_v2'].includes(run.plan.selection_policy));
  } catch (error) {
    if (selectedRunId === run.id) { document.querySelector('#review-summary').textContent = error.message; }
  }
}

function renderRun(run, scroll = false) {
  const { plan, assessment, articles } = run;
  activeRun = run;
  selectedRunId = run.id;
  evidenceReviews = new Map();
  document.querySelector('#watch-current').textContent = savedWatches.some((watch) => watch.seed_run_id === run.id) ? '已保存每日监测' : '保存为每日监测';
  document.querySelector('#result-topic').textContent = plan.topic;
  const analysisModel = plan.analysis_provider?.model ? ` · 分析模型 ${plan.analysis_provider.model}` : '';
  const currentSelection = ['longform_original_v1', 'representative_v2'].includes(plan.selection_policy);
  document.querySelector('#run-meta').textContent = `${readableDate(run.created_at)} · ${run.plan_mode}${analysisModel} · ${articles.length} 条${currentSelection ? '代表材料' : '历史候选'}${plan.selection_policy === 'representative_v2' && run.candidates?.length ? ` / ${run.candidates.length} 条候选` : ''} · ${run.storage || '本地备份'}`;
  const currentStandard = Boolean(plan.coverage && plan.coverage.target === 30 && plan.coverage.required_risk_points === 4);
  const planNotes = currentStandard
    ? [`时段：${plan.time_note}`, `语种：${(plan.languages || []).join(' · ')}`, `目标：${plan.target_count}–${plan.coverage?.selected_max || 50} 篇独立长篇代表材料${plan.new_count !== undefined ? ` · 本次新增 ${plan.new_count}` : ''}`]
    : [`时段：${plan.time_note}`, ...(plan.queries || []).map((query) => `检索：${query}`)];
  document.querySelector('#plan-strip').innerHTML = planNotes.map((item) => `<span>${escapeHtml(item)}</span>`).join('');
  renderCoverage(currentSelection ? plan : {});
  document.querySelector('#legacy-note').hidden = currentSelection;
  document.querySelector('#legacy-note').textContent = currentStandard
    ? '这份档案生成于长篇体裁与同稿去重筛选启用之前，未按当前入库标准复核。'
    : '这份档案由旧版少量检索流程生成，未按当前的30篇独立长篇材料与4个风险点标准执行。';
  document.querySelector('#legacy-insights').hidden = currentStandard;
  document.querySelector('#analysis-grid').classList.toggle('single', currentStandard);
  document.querySelector('#risk-label').textContent = currentStandard ? '综合风险（初步）' : '综合风险';
  document.querySelector('#risk-level').textContent = assessment.risk_level;
  document.querySelector('#risk-summary').textContent = assessment.summary || '暂无总结。';
  document.querySelector('#risk-reason').textContent = assessment.risk_reason || '暂无原因说明。';
  document.querySelector('#risk-rubric').textContent = assessment.risk_rubric || '';
  document.querySelector('#actions').innerHTML = (assessment.actions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('') || '<li>建议人工复核。</li>';
  document.querySelector('#limitations').innerHTML = (assessment.limitations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('') || '<li>暂无补充说明。</li>';
  document.querySelector('#insights').innerHTML = renderInsights(assessment);
  document.querySelector('#source-list-title').textContent = currentSelection ? '入选新闻列表' : '历史候选新闻列表';
  renderRiskSections(assessment, articles, currentStandard, currentSelection);
  renderEvidenceWorkbench(run);
  renderClaimTimeline(run);
  renderViewpoints(assessment, articles);
  renderSources(articles);
  candidateVisibleLimit = 80;
  document.querySelector('#candidate-search').value = '';
  renderCandidates(run);
  renderTrends(run);
  renderArchiveHints(plan);
  results.hidden = false;
  updateArchiveSelection();
  loadEvidenceReviews(run);
  if (scroll) { results.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
}

function updateArchiveSelection() {
  archiveList.querySelectorAll('[data-run-id]').forEach((button) => {
    const expanded = !results.hidden && button.dataset.runId === selectedRunId;
    button.setAttribute('aria-expanded', String(expanded));
    button.querySelector('.archive-open').textContent = expanded ? '收起 ↑' : '展开 ↗';
  });
}

function closeRun(scroll = false) {
  selectedRunId = null;
  activeRun = null;
  results.hidden = true;
  updateArchiveSelection();
  if (scroll) { document.querySelector('#archive-title').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
}

function archiveSearchText(run) {
  return [run.task, run.plan?.topic, run.assessment?.summary,
    ...(run.assessment?.risk_points || []).map((point) => point.heading),
    ...(run.candidates?.length ? run.candidates : run.articles || []).flatMap((article) => [article.title, article.source])]
    .filter(Boolean).join(' ').toLocaleLowerCase();
}

function updateArchiveNavigation() {
  const max = archiveList.scrollWidth - archiveList.clientWidth;
  archivePrev.disabled = max < 2 || archiveList.scrollLeft < 2;
  archiveNext.disabled = max < 2 || archiveList.scrollLeft >= max - 2;
}

function renderArchive(resetScroll = false) {
  const previousScroll = archiveList.scrollLeft;
  const query = archiveSearch.value.trim().toLocaleLowerCase();
  const runs = query ? savedRuns.filter((run) => archiveSearchText(run).includes(query)) : savedRuns;
  if (selectedRunId && !runs.some((run) => run.id === selectedRunId)) { closeRun(); }
  archiveMatchCount.textContent = query ? `找到 ${runs.length} / ${savedRuns.length} 份` : `${savedRuns.length} 份 · 左右滑动浏览`;
  archiveList.innerHTML = runs.map((run) => `
    <button type="button" class="archive-item" data-run-id="${escapeHtml(run.id)}" aria-controls="results" aria-expanded="${!results.hidden && run.id === selectedRunId}">
      <span class="archive-item-top"><span>${escapeHtml(readableDate(run.created_at))}</span><strong class="risk-chip">${['longform_original_v1', 'representative_v2'].includes(run.plan.selection_policy) ? (run.plan.coverage?.structure_met ? '结构达标 · 待核验' : '待补') : '旧版记录'}</strong></span>
      <strong class="archive-topic">${escapeHtml(run.plan.topic || run.task)}</strong>
      <span class="archive-summary">${escapeHtml(run.assessment.summary || '暂无研判摘要。')}</span>
      <span class="archive-item-bottom"><span>${run.articles.length} 条${['longform_original_v1', 'representative_v2'].includes(run.plan.selection_policy) ? '代表材料' : '历史候选'}${run.plan.selection_policy === 'representative_v2' && run.candidates?.length ? ` / ${run.candidates.length} 候选` : ''}</span><span class="archive-open">${run.id === selectedRunId && !results.hidden ? '收起 ↑' : '展开 ↗'}</span></span>
    </button>
  `).join('') || `<p class="archive-empty">${query ? '没有匹配的档案，试试其他议题、新闻标题或来源。' : '还没有研判记录。提交上方任务后，抓取的新闻和研判会显示在这里。'}</p>`;
  archiveList.scrollLeft = resetScroll ? 0 : previousScroll;
  requestAnimationFrame(updateArchiveNavigation);
}

async function loadHistory(preferredId = null, scroll = false) {
  try {
    const response = await fetch('/api/records', { cache: 'no-store' });
    if (!response.ok) { throw new Error('读取历史记录失败。'); }
    const data = await response.json();
    savedRuns = Array.isArray(data.records) ? data.records : [];
    if (preferredId) { archiveSearch.value = ''; }
    const selected = savedRuns.find((run) => run.id === preferredId)
      || savedRuns.find((run) => run.id === selectedRunId);
    archiveState.textContent = data.warning || `${savedRuns.length} 份档案 · ${data.source === 'mysql' ? 'MySQL' : '本地备份'}`;
    if (selected) { renderRun(selected, scroll); }
    else { closeRun(); }
    renderArchive();
  } catch (error) {
    archiveState.textContent = error.message || '档案读取失败';
  }
}

async function checkConfig() {
  try {
    const responses = await Promise.all([
      fetch('/api/deepseek/config', { cache: 'no-store' }),
      fetch('/api/analysis/config', { cache: 'no-store' }),
      fetch('/api/mysql/config', { cache: 'no-store' }),
    ]);
    const [deepseek, analysis, mysql] = await Promise.all(responses.map((response) => response.json()));
    if (responses.some((response) => !response.ok)) { throw new Error('配置状态读取失败'); }
    const missing = [];
    if (!deepseek.configured) { missing.push('DeepSeek 检索'); }
    if (!analysis.configured) { missing.push('分析模型 API'); }
    if (!mysql.configured) { missing.push('SQL 入库'); }
    else if (!mysql.connected) { missing.push('SQL 连接异常'); }
    state.textContent = missing.length ? `待处理：${missing.join('、')}` : '三项配置已完成';
    state.className = `system-state ${missing.length ? 'warning' : 'ready'}`;
    if (!deepseek.configured) { showMessage('请打开配置中心填写 DeepSeek API Key。', 'info'); }
  } catch (error) {
    state.textContent = '配置状态读取失败';
    state.className = 'system-state warning';
  }
}

function renderJobProgress(detail = {}) {
  jobProgress.hidden = false;
  const percent = Math.max(0, Math.min(100, Number(detail.percent) || 0));
  const names = { plan: '规划检索式', search: '检索来源', fetch: '提取正文', screen: '筛选与去重', assess: '生成研判', save: '保存入库', done: '已完成' };
  document.querySelector('#progress-stage').textContent = names[detail.stage] || '检索进行中';
  document.querySelector('#progress-percent').textContent = `${percent}%`;
  document.querySelector('#progress-fill').style.width = `${percent}%`;
  document.querySelector('#progress-track').setAttribute('aria-valuenow', String(percent));
  document.querySelector('#progress-message').textContent = detail.message || '检索进行中…';
  const stats = [];
  if (Number.isFinite(detail.done) && Number.isFinite(detail.total)) { stats.push(`当前阶段 ${detail.done}/${detail.total}`); }
  if (Number.isFinite(detail.qualified)) { stats.push(`入选 ${detail.qualified}/30`); }
  if (Number.isFinite(detail.duplicates)) { stats.push(`同稿 ${detail.duplicates}`); }
  if (Number.isFinite(detail.excluded)) { stats.push(`排除 ${detail.excluded}`); }
  document.querySelector('#progress-stats').textContent = stats.join(' · ');
}

async function loadBrowserImports() {
  const selected = new Set([...browserImportList.querySelectorAll('input:checked')].map((input) => input.value));
  try {
    const response = await fetch('/api/browser/imports', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '无法读取浏览器导入材料。'); }
    document.querySelector('#browser-import-token').textContent = data.pairing_token;
    browserImportList.innerHTML = (data.imports || []).map((item) => `
      <label class="browser-import-item"><input type="checkbox" value="${escapeHtml(item.id)}" ${selected.has(item.id) ? 'checked' : ''}>
        <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.source)} · ${escapeHtml(item.published_at || '发布日期待核')} · ${item.content_length || 0} 字 · ${escapeHtml(item.language || '语种待核')} · ${['defuddle', 'clean_dom', 'page_structured_data'].includes(item.method) ? '页面清理正文' : '浏览器正文'} · ${item.stored_in_sql ? '已入 SQL' : '本机待入库'}</small></span></label>
    `).join('') || '<p class="archive-empty">尚未导入浏览器文章。加载扩展后，在文章页面点击扩展图标。</p>';
    document.querySelector('#browser-import-state').textContent = `${(data.imports || []).length} 篇浏览器导入材料；${data.warning || '已入 SQL 的文章可跨浏览器和服务重启保留。'} 勾选后可加入下一轮研判。`;
  } catch (error) {
    browserImportList.textContent = error.message || '浏览器导入列表暂不可用。';
  }
}

document.querySelector('#browser-import-refresh').addEventListener('click', loadBrowserImports);
document.querySelector('#browser-copy-token').addEventListener('click', async () => {
  const token = document.querySelector('#browser-import-token').textContent;
  try {
    await navigator.clipboard.writeText(token);
    document.querySelector('#browser-import-state').textContent = '配对码已复制；请粘贴到本项目浏览器扩展。';
  } catch (_error) {
    document.querySelector('#browser-import-state').textContent = '请手动复制配对码。';
  }
});
loadBrowserImports();
window.addEventListener('focus', loadBrowserImports);

async function followJob(jobId) {
  runButton.disabled = true;
  runButton.textContent = '检索进行中…';
  renderJobProgress({ stage: 'plan', percent: 0, message: '正在连接任务…' });
  try {
    for (;;) {
      const response = await fetch(`/api/jobs/${jobId}`, { cache: 'no-store' });
      const job = await response.json();
      if (!response.ok) { throw new Error(job.error || '任务状态读取失败。'); }
      renderJobProgress(job.progress_detail || { message: job.progress });
      if (job.status === 'error') {
        sessionStorage.removeItem('suyuan-active-job');
        throw new Error(job.error || '检索任务失败。');
      }
      if (job.status === 'done') {
        sessionStorage.removeItem('suyuan-active-job');
        hideMessage();
        renderRun(job.result, true);
        await loadHistory(job.result.id);
        await loadWatches();
        setTimeout(() => { jobProgress.hidden = true; }, 3000);
        return;
      }
      showMessage(job.progress || '检索进行中…', 'info');
      await new Promise((resolve) => setTimeout(resolve, 1400));
    }
  } catch (error) {
    showMessage(error.message || '请求失败，请检查本地终端日志。');
    document.querySelector('#progress-stage').textContent = '任务失败';
  } finally {
    runButton.disabled = false;
    runButton.innerHTML = '开始检索与研判 <span>↗</span>';
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const task = taskInput.value.trim();
  if (task.length < 8) {
    showMessage('请至少写明一个议题和关注范围。');
    taskInput.focus();
    return;
  }
  showMessage('正在启动检索…', 'info');
  runButton.disabled = true;
  runButton.textContent = '检索进行中…';
  try {
    const payload = {
      task,
      languages: form.elements.languages.value.trim(),
      start_date: form.elements.start_date.value,
      end_date: form.elements.end_date.value,
      browser_import_ids: [...browserImportList.querySelectorAll('input:checked')].map((input) => input.value),
    };
    const response = await fetch('/api/run/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || '请求失败。');
    }
    sessionStorage.setItem('suyuan-active-job', result.job_id);
    await followJob(result.job_id);
  } catch (error) {
    showMessage(error.message || '请求失败，请检查本地终端日志。');
    runButton.disabled = false;
    runButton.innerHTML = '开始检索与研判 <span>↗</span>';
  }
});

const activeJob = sessionStorage.getItem('suyuan-active-job');
if (activeJob) { followJob(activeJob); }

document.querySelectorAll('[data-example]').forEach((button) => {
  button.addEventListener('click', () => {
    taskInput.value = button.dataset.example || '';
    taskInput.focus();
  });
});

checkConfig();
archiveList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-run-id]');
  if (!button) { return; }
  const run = savedRuns.find((item) => item.id === button.dataset.runId);
  if (!run) { return; }
  if (selectedRunId === run.id && !results.hidden) { closeRun(); }
  else { renderRun(run, true); }
});
archiveSearch.addEventListener('input', () => renderArchive(true));
archiveList.addEventListener('scroll', updateArchiveNavigation, { passive: true });
window.addEventListener('resize', updateArchiveNavigation);
archivePrev.addEventListener('click', () => archiveList.scrollBy({ left: -archiveList.clientWidth * 0.8, behavior: 'smooth' }));
archiveNext.addEventListener('click', () => archiveList.scrollBy({ left: archiveList.clientWidth * 0.8, behavior: 'smooth' }));
document.querySelector('#close-results').addEventListener('click', () => closeRun(true));
document.querySelector('#candidate-search').addEventListener('input', () => { candidateVisibleLimit = 80; if (activeRun) { renderCandidates(activeRun); } });
document.querySelector('#candidate-more').addEventListener('click', () => { candidateVisibleLimit += 80; if (activeRun) { renderCandidates(activeRun); } });
document.querySelector('#review-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-save-review]');
  if (!button || !activeRun) { return; }
  const card = button.closest('[data-passage-id]');
  const status = card.querySelector('select').value;
  const run = activeRun;
  button.disabled = true;
  try {
    const response = await fetch(`/api/evidence-reviews/${run.id}`, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({passage_id:card.dataset.passageId,
        status, note:card.querySelector('input').value})});
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '保存失败'); }
    if (selectedRunId !== run.id) { return; }
    evidenceReviews.set(data.review.passage_id, data.review);
    renderEvidenceWorkbench(run);
    renderRiskSections(run.assessment, run.articles, !document.querySelector('#risk-sections').hidden,
      ['longform_original_v1','representative_v2'].includes(run.plan.selection_policy));
  } catch (error) { document.querySelector('#review-summary').textContent = error.message; button.disabled = false; }
});
loadHistory();
loadSearchLanguages();

function renderWatches() {
  const panel = document.querySelector('#watch-list');
  document.querySelector('#watch-state').textContent = `${savedWatches.length} 个议题`;
  panel.innerHTML = savedWatches.map((watch) => `
    <article class="watch-item"><div><strong>${escapeHtml(watch.topic)}</strong><p>${watch.enabled ? `每天 ${escapeHtml(watch.daily_time)} 自动检索 · 下次 ${escapeHtml(readableDate(watch.next_run_at))}` : '已暂停自动检索'}${!watch.last_run_id ? ' · 首次运行将重建代表材料库' : ''}</p>${watch.last_alert ? `<p class="watch-alert">${escapeHtml(watch.last_alert.message)}</p>` : ''}</div><div class="watch-actions"><button type="button" data-watch-id="${escapeHtml(watch.id)}" data-action="run" ${watch.enabled ? '' : 'disabled'}>${watch.last_run_id ? '立即增量更新' : '立即建立基线'}</button><button type="button" data-watch-id="${escapeHtml(watch.id)}" data-action="toggle">${watch.enabled ? '暂停' : '启用'}</button></div></article>
  `).join('') || '<p class="archive-empty">打开一份研判档案后，可将它保存为每日监测议题。</p>';
}

async function loadWatches() {
  try {
    const previousRuns = savedWatches.map((watch) => watch.last_run_id).join(',');
    const response = await fetch('/api/watches', { cache: 'no-store' });
    if (!response.ok) { throw new Error('读取监测议题失败。'); }
    const data = await response.json();
    savedWatches = data.watches || [];
    renderWatches();
    if (activeRun) { renderTrends(activeRun); renderClaimTimeline(activeRun); }
    if (previousRuns && previousRuns !== savedWatches.map((watch) => watch.last_run_id).join(',')) {
      await loadHistory();
    }
  } catch (error) {
    document.querySelector('#watch-state').textContent = error.message;
  }
}

document.querySelector('#watch-current').addEventListener('click', async () => {
  if (!activeRun) { return; }
  const button = document.querySelector('#watch-current');
  button.disabled = true;
  try {
    const response = await fetch('/api/watches', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: activeRun.id, daily_time: document.querySelector('#watch-time').value }) });
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '保存监测议题失败。'); }
    await loadWatches();
    button.textContent = '已保存每日监测';
  } catch (error) {
    showMessage(error.message);
  } finally { button.disabled = false; }
});

document.querySelector('#watch-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-watch-id]');
  if (!button) { return; }
  button.disabled = true;
  try {
    const watch = savedWatches.find((item) => item.id === button.dataset.watchId);
    const response = await fetch(`/api/watches/${button.dataset.watchId}/${button.dataset.action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(button.dataset.action === 'toggle' ? { enabled: !watch.enabled } : {}),
    });
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || '监测任务操作失败。'); }
    await loadWatches();
    if (data.job_id) { await followJob(data.job_id); }
  } catch (error) { showMessage(error.message); }
  finally { button.disabled = false; }
});
loadWatches();
setInterval(loadWatches, 60_000);

