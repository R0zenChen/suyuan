const $ = (selector) => document.querySelector(selector);
async function request(path, body) {
  const response = await fetch(path, body === undefined ? undefined : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function updateCounts(counts) {
  $('#stats').innerHTML = [['news','新闻'],['clip','剪藏'],['social','社媒'],['paper','论文']].map(([key,label]) => `<div><strong>${Number(counts[key] || 0)}</strong><span>${label}</span></div>`).join('');
}
function setStatus(selector, text, error=false) { const el=$(selector); el.textContent=text; el.classList.toggle('error',error); }
let vectorPollTimer;
function showVectorStatus(counts, job) {
  const ready=Number(counts.news_social_vectors || 0), total=Number(counts.news_social_chunks || 0);
  $('#vector-status').textContent = `新闻／社媒语义索引 ${ready}/${total} 段${job?.status === 'running' ? ` · ${job.message}` : job?.status === 'error' ? ` · ${job.message}` : ''}`;
  clearTimeout(vectorPollTimer);
  if (job?.status === 'running') { vectorPollTimer=setTimeout(pollVectorJob,2000); }
}
async function pollVectorJob() {
  try { const data=await request('/api/rag/vector-job'); showVectorStatus(data.counts,data.job); }
  catch(error) { $('#vector-status').textContent=error.message; }
}
async function loadStatus() { try { const data=await request('/api/rag/status'); updateCounts(data.counts); showVectorStatus(data.counts,data.vector_job); } catch(error) { setStatus('#ask-status',error.message,true); } }
$('#collect-form').addEventListener('submit', async event => {
  event.preventDefault(); const button=$('#collect-button'); button.disabled=true; setStatus('#collect-status','正在采集公开帖子并写入 SQL…');
  try { const data=await request('/api/social/collect',{query:$('#social-query').value,mastodon_tag:$('#mastodon-tag').value}); updateCounts(data.counts); showVectorStatus(data.counts,data.vector_job); setStatus('#collect-status',`已处理 ${data.saved} 条（重复帖子会更新），本次新建或更新索引 ${data.indexed.indexed} 条。${(data.warnings || []).join(' ')}`); }
  catch(error){setStatus('#collect-status',error.message,true)} finally{button.disabled=false}
});
$('#ask-form').addEventListener('submit', async event => {
  event.preventDefault(); const button=$('#ask-button'); button.disabled=true; setStatus('#ask-status','正在检索本地证据并请求分析模型…'); $('#answer').hidden=true; $('#sources').replaceChildren();
  try { const data=await request('/api/rag/ask',{question:$('#question').value}); updateCounts(data.counts); $('#answer').textContent=data.answer; $('#answer').hidden=false; setStatus('#ask-status',`检索到 ${data.sources.length} 条证据。`); for(const item of data.sources){const card=document.createElement('div');card.className='source';const label=document.createElement('small');const kind=item.kind==='paper'?'论文':item.kind==='social'?'社媒':item.kind==='clip'?'浏览器剪藏':'新闻';label.textContent=`[${item.citation}] ${kind} · ${item.source || '未知来源'}${item.page_no?' · 第'+item.page_no+'页':''}`;const link=document.createElement('a');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=item.title || item.excerpt.slice(0,90) || item.url;const meta=document.createElement('p');meta.textContent=[item.metadata?.authors,item.published_at].filter(Boolean).join(' · ');const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='查看入库证据摘录';const excerpt=document.createElement('p');excerpt.textContent=item.excerpt;details.append(summary,excerpt);card.append(label,link,meta,details);$('#sources').append(card)} }
  catch(error){setStatus('#ask-status',error.message,true)} finally{button.disabled=false}
});
$('#reindex-button').addEventListener('click',async()=>{const button=$('#reindex-button');button.disabled=true;$('#reindex-status').textContent='正在同步…';try{const data=await request('/api/rag/reindex',{});updateCounts(data.counts);showVectorStatus(data.counts,data.vector_job);$('#reindex-status').textContent=`完成：更新 ${data.result.indexed} 条；${data.paper_jobs.length} 篇论文在后台建立向量。`;await loadPapers();if(data.paper_jobs.length) pollPaperJob(data.paper_jobs[0]);}catch(error){$('#reindex-status').textContent=error.message}finally{button.disabled=false}});
$('#vectorize-button').addEventListener('click',async()=>{const button=$('#vectorize-button');button.disabled=true;try{const data=await request('/api/rag/vectorize',{});showVectorStatus(data.counts,data.vector_job);}catch(error){$('#vector-status').textContent=error.message}finally{button.disabled=false}});
const paperFile = $('#paper-file');
const paperDropzone = $('#paper-dropzone');
function showSelectedPaper() {
  const file = paperFile.files[0];
  $('#paper-file-label').textContent = file ? `${file.name} · ${(file.size/1048576).toFixed(1)} MB` : '选择或拖入论文 PDF';
}
paperFile.addEventListener('change', showSelectedPaper);
paperDropzone.addEventListener('dragover', event => { event.preventDefault(); paperDropzone.classList.add('drag-over'); });
paperDropzone.addEventListener('dragleave', () => paperDropzone.classList.remove('drag-over'));
paperDropzone.addEventListener('drop', event => {
  event.preventDefault(); paperDropzone.classList.remove('drag-over');
  if (event.dataTransfer.files.length) { paperFile.files = event.dataTransfer.files; showSelectedPaper(); }
});
function readPaper(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',',2)[1]);
    reader.onerror = () => reject(new Error('无法读取本机 PDF 文件。'));
    reader.readAsDataURL(file);
  });
}
async function loadPapers() {
  const data = await request('/api/papers');
  $('#paper-count').textContent = `${data.papers.length} 篇`;
  const list = $('#paper-list'); list.replaceChildren();
  if (!data.papers.length) {
    const empty=document.createElement('p'); empty.className='note'; empty.textContent='还没有论文。选择 PDF 后，正文与页码会保存在本机 SQL。'; list.append(empty); return data;
  }
  for (const paper of data.papers) {
    const card=document.createElement('article'); card.className='paper-item';
    const link=document.createElement('a'); link.href=paper.url; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=paper.title;
    const meta=document.createElement('p'); meta.textContent=[paper.authors,paper.year,paper.metadata?.journal,`${paper.page_count} 页`,`${paper.text_chars.toLocaleString()} 字`].filter(Boolean).join(' · ');
    const status=document.createElement('span'); status.className='vector-state'+(paper.vector_status.complete?'':' pending');
    status.textContent=paper.vector_status.complete ? `向量索引完成 · ${paper.vector_status.ready} 段` : `已入库 · 向量 ${paper.vector_status.ready}/${paper.vector_status.total} 段`;
    card.append(link,meta,status);
    if (paper.doi) {
      const doiRow=document.createElement('p'); doiRow.className='doi-row';
      const doi=document.createElement('a'); doi.href=`https://doi.org/${encodeURIComponent(paper.doi)}`; doi.target='_blank'; doi.rel='noopener noreferrer'; doi.textContent=`DOI: ${paper.doi}`;
      doiRow.append(doi); card.append(doiRow);
    }
    if (paper.source_url) { const origin=document.createElement('a'); origin.href=paper.source_url; origin.target='_blank'; origin.rel='noopener noreferrer'; origin.textContent='查看论文来源 ↗'; card.append(origin); }
    if (!paper.vector_status.complete) {
      const retry=document.createElement('button'); retry.type='button'; retry.textContent='继续向量化';
      retry.addEventListener('click',async()=>{retry.disabled=true;try{const job=await request(`/api/papers/${paper.id}/vectorize`,{});pollPaperJob(job.job_id);}catch(error){setStatus('#paper-status',error.message,true)}finally{retry.disabled=false}});
      card.append(retry);
    }
    list.append(card);
  }
  return data;
}
async function pollPaperJob(jobId) {
  $('#paper-progress').hidden=false;
  try {
    const job=await request(`/api/papers/jobs/${jobId}`);
    const percent=job.total ? Math.max(4,Math.round(100*job.done/job.total)) : 4;
    $('#paper-progress-fill').style.width=`${percent}%`;
    setStatus('#paper-status',job.message,job.status==='error');
    if (job.status==='running') { setTimeout(()=>pollPaperJob(jobId),1200); return; }
    if (job.status==='error') { sessionStorage.removeItem('paperImportJob'); $('#paper-progress').hidden=true; return; }
    if (job.result?.paper) {
      sessionStorage.removeItem('paperImportJob');
      updateCounts(job.result.counts);
      const paper=job.result.paper;
      const ocr=paper.page_methods.filter(method=>method==='local_ocr').length;
      const vision=paper.page_methods.filter(method=>method==='vision').length;
      setStatus('#paper-status',`${job.result.duplicate?'已更新':'已入库'}《${paper.title}》· ${paper.page_count} 页；本地 OCR ${ocr} 页、多模态 ${vision} 页。${paper.ocr_warnings.join(' ')} 正在建立向量索引…`);
      await loadPapers();
      if (job.result.job_id) { pollPaperJob(job.result.job_id); return; }
    }
    $('#paper-progress').hidden=true; await loadPapers();
  } catch(error) { $('#paper-progress').hidden=true; setStatus('#paper-status',error.message,true); }
}
$('#paper-form').addEventListener('submit',async event=>{
  event.preventDefault(); const file=paperFile.files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf') || file.size>15000000) { setStatus('#paper-status','请选择不超过 15 MB 的 PDF 文件。',true); return; }
  const button=$('#paper-button'); button.disabled=true; setStatus('#paper-status','正在识别 PDF 正文并写入 SQL…');
  try {
    const encoded=await readPaper(file);
    const data=await request('/api/papers/import',{filename:file.name,pdf_base64:encoded,title:$('#paper-title').value,authors:$('#paper-authors').value,year:$('#paper-year').value,use_vision:$('#paper-use-vision').checked});
    sessionStorage.setItem('paperImportJob',data.job_id);
    pollPaperJob(data.job_id);
  } catch(error) { setStatus('#paper-status',error.message,true); } finally { button.disabled=false; }
});
loadStatus();
loadPapers().then(data=>{const active=data.papers.find(paper=>paper.active_job_id);if(active)pollPaperJob(active.active_job_id)}).catch(error=>setStatus('#paper-status',error.message,true));
if (sessionStorage.getItem('paperImportJob')) { pollPaperJob(sessionStorage.getItem('paperImportJob')); }

const mwForm=$('#meltwater-form');
const mwButton=$('#meltwater-button');
function chinaDate(offset=0){const now=new Date();const local=new Date(now.toLocaleString('en-US',{timeZone:'Asia/Shanghai'}));local.setDate(local.getDate()+offset);return `${local.getFullYear()}-${String(local.getMonth()+1).padStart(2,'0')}-${String(local.getDate()).padStart(2,'0')}`;}
$('#meltwater-from').value=chinaDate(-39);
$('#meltwater-to').value=chinaDate();
async function loadMeltwaterStatus(){
  try{
    const data=await request('/api/meltwater/status');
    const accounts=data.accounts||[];
    $('#meltwater-connection').textContent=data.connected?`融文研究台已连接 · ${accounts.filter(a=>a.status==='已连接').length} 个账号就绪`:(data.error||'融文研究台未连接');
    $('#meltwater-connection').classList.toggle('error',!data.connected);
    const select=$('#meltwater-account'), selected=select.value;select.replaceChildren();
    for(const account of accounts){const option=document.createElement('option');option.value=account.key;option.textContent=`${account.email||account.label} · ${account.status}`;select.append(option);}
    const manager=$('#meltwater-accounts');manager.replaceChildren();
    for(const account of accounts){
      const row=document.createElement('div');row.className='meltwater-account-row';
      const heading=document.createElement('div');heading.className='meltwater-account-heading';
      const name=document.createElement('strong');name.textContent=account.label;
      const state=document.createElement('span');state.textContent=account.status;heading.append(name,state);
      const controls=document.createElement('div');controls.className='meltwater-account-controls';
      const email=document.createElement('input');email.type='email';email.placeholder='账号邮箱（可补填）';email.value=account.email||'';email.setAttribute('aria-label',`${account.label}的邮箱`);
      const save=document.createElement('button');save.type='button';save.textContent='保存邮箱';
      save.addEventListener('click',async()=>{try{await request(`/api/meltwater/accounts/${account.key}/email`,{email:email.value.trim()});setStatus('#meltwater-status','账号邮箱已保存。');loadMeltwaterStatus();}catch(error){setStatus('#meltwater-status',error.message,true)}});
      const connect=document.createElement('button');connect.type='button';connect.textContent='连接';
      connect.addEventListener('click',async()=>{connect.disabled=true;try{await request(`/api/meltwater/accounts/${account.key}/connect`,{});setStatus('#meltwater-status','正在连接融文账号，请在官方页面完成登录。');setTimeout(loadMeltwaterStatus,2500);}catch(error){setStatus('#meltwater-status',error.message,true)}finally{connect.disabled=false}});
      controls.append(email,save,connect);
      if(account.key!=='default'){
        const remove=document.createElement('button');remove.type='button';remove.className='meltwater-account-delete';remove.textContent='删除';
        remove.addEventListener('click',async()=>{if(!confirm(`删除“${account.label}”及其独立浏览器资料？`))return;remove.disabled=true;try{await request(`/api/meltwater/accounts/${account.key}/delete`,{});setStatus('#meltwater-status','融文账号已删除。');loadMeltwaterStatus();}catch(error){setStatus('#meltwater-status',error.message,true);remove.disabled=false}});
        controls.append(remove);
      }
      row.append(heading,controls);manager.append(row);
    }
    if(accounts.filter(a=>a.accountId).length>=2){const option=document.createElement('option');option.value='pool';option.textContent='已连接账号并行';select.append(option);}
    if([...select.options].some(option=>option.value===selected))select.value=selected;
    mwButton.disabled=!data.connected||!data.sql_configured;
    $('#meltwater-add-account').disabled=!data.connected;
    if(!data.sql_configured)setStatus('#meltwater-status','先在配置中心连接 MySQL，才能下载并持久入库。',true);
  }catch(error){setStatus('#meltwater-connection',error.message,true);mwButton.disabled=true;}
}
async function loadMeltwaterDownloads(){
  try{
    const data=await request('/api/meltwater/downloads');const list=$('#meltwater-downloads');list.replaceChildren();
    $('#meltwater-count').textContent=`${data.downloads.length} 次`;
    if(!data.downloads.length){const p=document.createElement('p');p.className='note';p.textContent='尚无已完成的融文下载。';list.append(p);return;}
    for(const item of data.downloads){const card=document.createElement('article');card.className='meltwater-download';const title=document.createElement('strong');title.textContent=item.query.length>170?item.query.slice(0,170)+'…':item.query;title.title=item.query;const meta=document.createElement('small');meta.textContent=`${item.from}—${item.to} · ${item.kind==='news'?'仅新闻':'全部类型'} · ${item.downloaded}/${item.total} 条 · ${item.created_at.slice(0,16)}`;const link=document.createElement('a');link.href=`/api/meltwater/downloads/${item.id}/export`;link.textContent='下载记录 JSONL ↗';card.append(title,meta,link);list.append(card);}
  }catch(error){setStatus('#meltwater-status',error.message,true);}
}
$('#meltwater-refresh').addEventListener('click',()=>{loadMeltwaterStatus();loadMeltwaterDownloads();});
$('#meltwater-add-account').addEventListener('click',async()=>{const button=$('#meltwater-add-account');button.disabled=true;try{await request('/api/meltwater/accounts',{label:''});setStatus('#meltwater-status','已打开融文官方登录页。完成登录后点击“刷新连接”查看账号状态。');setTimeout(loadMeltwaterStatus,2500);}catch(error){setStatus('#meltwater-status',error.message,true)}finally{button.disabled=false}});
$('#meltwater-ai-button').addEventListener('click',async()=>{const button=$('#meltwater-ai-button');button.disabled=true;setStatus('#meltwater-status','正在起草融文布尔检索式…');try{const data=await request('/api/meltwater/ai-query',{subject:$('#meltwater-subject').value,context:$('#meltwater-context').value,languages:$('#meltwater-languages').value});$('#meltwater-query').value=data.query;setStatus('#meltwater-status',`检索式已起草，请检查后再运行。${data.rationale||''}`);}catch(error){setStatus('#meltwater-status',error.message,true)}finally{button.disabled=false}});
async function pollMeltwaterJob(id){
  mwButton.disabled=true;$('#meltwater-progress').hidden=false;
  try{
    const job=await request(`/api/meltwater/jobs/${id}`);
    const progress=job.total_months?Math.round(job.completed_months/job.total_months*75):8;
    const percent=job.stage==='download'?80:job.stage==='sql'?88:job.stage==='index'?95:job.status==='done'?100:progress;
    $('#meltwater-progress-fill').style.width=`${Math.min(100,Math.max(5,percent))}%`;
    setStatus('#meltwater-status',`${job.message}${job.platform_total?` · 当前命中 ${job.platform_total} 条`:''}`,job.status==='error');
    if(job.status==='running'){setTimeout(()=>pollMeltwaterJob(id),1800);return;}
    sessionStorage.removeItem('meltwaterDownloadJob');mwButton.disabled=false;
    if(job.status==='done'){setStatus('#meltwater-status',`已核验 ${job.result.downloaded} 条，新增入库 ${job.result.new} 条，重复 ${job.result.existing} 条；本地索引更新 ${job.result.indexed.indexed} 条。`);await loadMeltwaterDownloads();await loadStatus();}
    else $('#meltwater-progress').hidden=true;
  }catch(error){sessionStorage.removeItem('meltwaterDownloadJob');mwButton.disabled=false;$('#meltwater-progress').hidden=true;setStatus('#meltwater-status',error.message,true);}
}
mwForm.addEventListener('submit',async event=>{event.preventDefault();mwButton.disabled=true;setStatus('#meltwater-status','正在发起融文全量下载…');try{const data=await request('/api/meltwater/search',{query:$('#meltwater-query').value,from:$('#meltwater-from').value,to:$('#meltwater-to').value,kind:$('#meltwater-kind').value,account_key:$('#meltwater-account').value});sessionStorage.setItem('meltwaterDownloadJob',data.job_id);pollMeltwaterJob(data.job_id);}catch(error){mwButton.disabled=false;setStatus('#meltwater-status',error.message,true)}});
loadMeltwaterStatus();loadMeltwaterDownloads();
if(sessionStorage.getItem('meltwaterDownloadJob'))pollMeltwaterJob(sessionStorage.getItem('meltwaterDownloadJob'));
