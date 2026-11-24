'use strict';

const deepseekForm = document.querySelector('#deepseek-form');
const analysisForm = document.querySelector('#analysis-form');
const dbForm = document.querySelector('#db-form');
const modelCatalogues = { deepseek: [], analysis: [] };
const effortLabels = { auto: '自动（服务默认）', none: '无思考 · none', minimal: '最少 · minimal', low: '低 · low', medium: '中 · medium', high: '高 · high', xhigh: '超高 · xhigh', max: '最大 · max' };
const officialEfforts = { deepseek: ['low', 'high', 'max'], openai: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'], anthropic: ['low', 'medium', 'high', 'xhigh', 'max'] };

function showTab(name) {
  const target = ['deepseek', 'analysis', 'mysql'].includes(name) ? name : 'deepseek';
  document.querySelectorAll('.settings-tab').forEach((button) => {
    const active = button.dataset.tab === target;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('.settings-panel').forEach((panel) => {
    panel.hidden = panel.id !== `panel-${target}`;
  });
  history.replaceState(null, '', `#${target}`);
}

document.querySelectorAll('.settings-tab').forEach((button) => button.addEventListener('click', () => showTab(button.dataset.tab)));
window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));

function setStatus(name, text, ready) {
  document.querySelector(`#${name === 'mysql' ? 'db' : name}-state`).textContent = text;
  document.querySelector(`#${name}-indicator`).className = `status-dot ${ready ? 'ready' : 'warning'}`;
}

function setFeedback(name, text, error = false) {
  const element = document.querySelector(`#${name === 'mysql' ? 'db' : name}-message`);
  element.textContent = text;
  element.classList.toggle('error', error);
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) { throw new Error(data.error || `请求失败（${response.status}）`); }
  return data;
}

async function withBusy(form, action) {
  const buttons = form.querySelectorAll('button');
  buttons.forEach((button) => { button.disabled = true; });
  try { await action(); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

function renderEffort(name, preferred) {
  const form = name === 'deepseek' ? deepseekForm : analysisForm;
  const format = name === 'deepseek' ? 'deepseek' : form.elements.format.value;
  const modelId = form.elements.model.value.trim();
  const model = modelCatalogues[name].find((item) => item.id === modelId);
  const levels = model?.effort_levels?.length ? model.effort_levels : officialEfforts[format];
  const effort = form.elements.reasoning_effort;
  const current = preferred || effort.value || 'auto';
  effort.replaceChildren();
  for (const level of ['auto', ...levels]) {
    const option = document.createElement('option');
    option.value = level;
    option.textContent = effortLabels[level] || level;
    effort.append(option);
  }
  effort.value = levels.includes(current) ? current : 'auto';
  const help = document.querySelector(`#${name}-effort-help`);
  if (model?.effort_levels?.length) {
    help.textContent = `${model.name || model.id}：按接口返回的支持档位显示${model.default_effort ? `，默认 ${model.default_effort}` : ''}。`;
  } else {
    help.textContent = `显示${format === 'deepseek' ? 'DeepSeek' : format === 'anthropic' ? 'Anthropic' : 'OpenAI'} 官方参数档位；当前服务未返回模型的档位信息，请用“测试连接”核对。`;
  }
}

async function fetchModels(name, quiet = false) {
  const form = name === 'deepseek' ? deepseekForm : analysisForm;
  if (!quiet) { setFeedback(name, '正在向服务读取可用模型…'); }
  await withBusy(form, async () => {
    try {
      const models = (await api(`/api/${name}/models`, Object.fromEntries(new FormData(form).entries()))).models;
      modelCatalogues[name] = models;
      const list = document.querySelector(`#${name}-model-list`);
      list.replaceChildren();
      models.forEach((model) => {
        const option = document.createElement('option');
        option.value = model.id;
        option.label = model.name;
        list.append(option);
      });
      renderEffort(name);
      if (!quiet) { setFeedback(name, models.length ? `已读取 ${models.length} 个模型。选择模型后，思考档位会同步更新。` : '接口返回了空模型列表，可以直接填写模型标识。'); }
    } catch (error) { if (!quiet) { setFeedback(name, error.message, true); } }
  });
}

async function loadDeepseek() {
  try {
    const response = await fetch('/api/deepseek/config', { cache: 'no-store' });
    const config = await response.json();
    if (!response.ok) { throw new Error(config.error); }
    deepseekForm.elements.model.value = config.model || 'deepseek-flash';
    deepseekForm.elements.api_key.value = '';
    deepseekForm.elements.api_key.placeholder = config.api_key_set ? '已保存；不更换可留空' : '填入 DeepSeek API Key';
    renderEffort('deepseek', config.reasoning_effort || 'auto');
    setStatus('deepseek', config.configured ? `已配置 · ${config.source}` : '尚未配置', config.configured);
    if (config.configured) { fetchModels('deepseek', true); }
  } catch (error) { setStatus('deepseek', error.message || '读取失败', false); }
}

async function loadAnalysis() {
  try {
    const response = await fetch('/api/analysis/config', { cache: 'no-store' });
    const config = await response.json();
    if (!response.ok) { throw new Error(config.error); }
    for (const name of ['format', 'base_url', 'model']) { analysisForm.elements[name].value = config[name] || (name === 'format' ? 'openai' : ''); }
    analysisForm.elements.api_key.value = '';
    analysisForm.elements.api_key.placeholder = config.api_key_set ? '已保存；不更换可留空' : '填入套餐提供的 Key';
    renderEffort('analysis', config.reasoning_effort || 'auto');
    setStatus('analysis', config.active_source || '未配置', config.configured || config.active_source === 'DeepSeek 默认分析');
    if (config.configured) { fetchModels('analysis', true); }
  } catch (error) { setStatus('analysis', error.message || '读取失败', false); }
}

async function loadDatabase() {
  try {
    const response = await fetch('/api/mysql/config', { cache: 'no-store' });
    const config = await response.json();
    if (!response.ok) { throw new Error(config.error); }
    for (const name of ['host', 'port', 'database', 'user']) { dbForm.elements[name].value = config[name] ?? dbForm.elements[name].value; }
    dbForm.elements.password.value = '';
    dbForm.elements.password.placeholder = config.configured ? '已保存；不更换可留空' : '填写数据库密码';
    setStatus('mysql', config.connected ? `已连接 · ${config.article_count} 条新闻` : (config.configured ? '已配置 · 连接失败' : '尚未连接'), config.connected);
  } catch (error) { setStatus('mysql', error.message || '读取失败', false); }
}

deepseekForm.elements.model.addEventListener('input', () => renderEffort('deepseek'));
analysisForm.elements.model.addEventListener('input', () => renderEffort('analysis'));
analysisForm.elements.format.addEventListener('change', () => { modelCatalogues.analysis = []; document.querySelector('#analysis-model-list').replaceChildren(); renderEffort('analysis'); });
analysisForm.elements.base_url.addEventListener('change', () => { modelCatalogues.analysis = []; document.querySelector('#analysis-model-list').replaceChildren(); renderEffort('analysis'); });
analysisForm.elements.base_url.addEventListener('blur', () => {
  if (analysisForm.elements.base_url.validity.valid && (analysisForm.elements.api_key.value || analysisForm.elements.api_key.placeholder.startsWith('已保存'))) { fetchModels('analysis', true); }
});
analysisForm.elements.api_key.addEventListener('change', () => { if (analysisForm.elements.base_url.validity.valid) { fetchModels('analysis', true); } });
deepseekForm.elements.api_key.addEventListener('change', () => fetchModels('deepseek', true));
document.querySelector('#deepseek-models').addEventListener('click', () => fetchModels('deepseek'));
document.querySelector('#analysis-models').addEventListener('click', () => fetchModels('analysis'));

deepseekForm.addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(deepseekForm, async () => {
    setFeedback('deepseek', '正在保存…');
    try { await api('/api/deepseek/config', Object.fromEntries(new FormData(deepseekForm).entries())); await loadDeepseek(); setFeedback('deepseek', '已保存。工作台的新任务将使用这份 DeepSeek 检索配置。'); }
    catch (error) { setFeedback('deepseek', error.message, true); }
  });
});
document.querySelector('#deepseek-test').addEventListener('click', () => withBusy(deepseekForm, async () => {
  setFeedback('deepseek', '正在调用一次最小检索…');
  try { await api('/api/deepseek/test', Object.fromEntries(new FormData(deepseekForm).entries())); setFeedback('deepseek', '连接成功，Search 接口已返回结果。本次测试会消耗少量额度。'); }
  catch (error) { setFeedback('deepseek', error.message, true); }
}));

analysisForm.addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(analysisForm, async () => {
    setFeedback('analysis', '正在保存…');
    try { await api('/api/analysis/config', Object.fromEntries(new FormData(analysisForm).entries())); await loadAnalysis(); setFeedback('analysis', '已保存并启用。后续规划、筛选与研判将使用此接口。'); }
    catch (error) { setFeedback('analysis', error.message, true); }
  });
});
document.querySelector('#analysis-test').addEventListener('click', () => withBusy(analysisForm, async () => {
  setFeedback('analysis', '正在测试模型响应与思考参数…');
  try { const data = await api('/api/analysis/test', Object.fromEntries(new FormData(analysisForm).entries())); setFeedback('analysis', `连接成功：${data.model} 已返回消息。本次测试会消耗少量模型额度。`); }
  catch (error) { setFeedback('analysis', error.message, true); }
}));
document.querySelector('#analysis-reset').addEventListener('click', () => withBusy(analysisForm, async () => {
  setFeedback('analysis', '正在恢复…');
  try { await api('/api/analysis/reset', {}); await loadAnalysis(); setFeedback('analysis', '已恢复 DeepSeek 默认分析。'); }
  catch (error) { setFeedback('analysis', error.message, true); }
}));

dbForm.addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(dbForm, async () => {
    setFeedback('mysql', '正在连接并建立数据表…');
    try { const data = await api('/api/mysql/config', Object.fromEntries(new FormData(dbForm).entries())); await loadDatabase(); setFeedback('mysql', `已连接；共 ${data.article_count} 条新闻，导入 ${data.imported_runs} 次旧研判。`); }
    catch (error) { setFeedback('mysql', error.message, true); }
  });
});
document.querySelector('#db-test').addEventListener('click', () => withBusy(dbForm, async () => {
  setFeedback('mysql', '正在测试数据库连接…');
  try { const data = await api('/api/mysql/test', Object.fromEntries(new FormData(dbForm).entries())); setFeedback('mysql', `连接成功：${data.version}`); }
  catch (error) { setFeedback('mysql', error.message, true); }
}));

Promise.all([loadDeepseek(), loadAnalysis(), loadDatabase()]);
