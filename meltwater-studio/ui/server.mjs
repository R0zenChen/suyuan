import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { isSea } from 'node:sea';
import { createAccountStore } from './accounts.mjs';
import * as api from './lib/meltwater-api.mjs';
import * as cdpModule from './lib/cdp.mjs';

const packaged = isSea();
const appRoot = packaged ? path.dirname(process.execPath) : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const here = packaged ? path.join(appRoot, 'ui') : path.join(appRoot, 'ui');
const dataRoot = process.env.MW_STUDIO_DATA_ROOT || path.join(appRoot, 'data');
const runsDir = path.join(dataRoot, 'runs');
fs.mkdirSync(runsDir, { recursive: true });
const settingsPath=path.join(dataRoot,'settings.json');
const settings=fs.existsSync(settingsPath)?JSON.parse(fs.readFileSync(settingsPath,'utf8')):{};
const accountId = process.env.MW_ACCOUNT_ID || settings.defaultAccountId || '';
process.env.CHROME_USER_DATA_DIR ||= cdpModule.chromeUserDataDir();
const accounts = createAccountStore({api,cdpModule,metadataFile:path.join(dataRoot,'accounts.json'),defaultAccountId:accountId,profileRoot:path.join(dataRoot,'profiles')});
const jobs = new Map();
let activeJob = null;
const PORT = Number(process.env.PORT || settings.port || 8789);
const aiModel=process.env.AI_MODEL||settings.aiModel||'';
const aiBaseUrl=process.env.AI_BASE_URL||settings.aiBaseUrl||'http://127.0.0.1:11434/v1';
const aiApiKey=process.env.AI_API_KEY||settings.aiApiKey||'';

const json = (res, code, value) => { res.writeHead(code, {'content-type':'application/json; charset=utf-8','cache-control':'no-store'}); res.end(JSON.stringify(value)); };
const safe = s => String(s || '').slice(0, 20000);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const today = () => { const p=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(x=>[x.type,x.value]));return `${p.year}-${p.month}-${p.day}`; };
const readBody = async req => { let s=''; for await (const chunk of req) { s+=chunk; if(s.length>200000) throw new Error('请求内容过长'); } return JSON.parse(s||'{}'); };
const count = (rows, key) => Object.entries(rows.reduce((m,x)=>{const k=key(x)||'未知';m[k]=(m[k]||0)+1;return m;},{})).sort((a,b)=>b[1]-a[1]);
const row = g => ({id:g.documentId||g.externalId||'',date:g.date||'',title:g.title||'',source:typeof g.source==='string'?g.source:(g.source?.name||''),country:g.country||'',language:g.language||'',sentiment:g.sentiment||'',type:g.informationType||'',url:g.url||g.originalUrl||'',excerpt:String(g.matchSentence||g.body||'').replace(/<[^>]+>/g,'').replace(/\s+/g,' ').slice(0,280)});
const monthWindows = (from,to,frozenEnd) => {
  const out=[];
  for(let y=Number(from.slice(0,4)),m=Number(from.slice(5,7));`${y}-${String(m).padStart(2,'0')}`<=to.slice(0,7);m++,(m>12?(m=1,y++):null)){
    const label=`${y}-${String(m).padStart(2,'0')}`;
    const last=new Date(Date.UTC(y,m,0)).getUTCDate();
    const a=label===from.slice(0,7)?from:`${label}-01`;
    const b=label===to.slice(0,7)?to:`${label}-${String(last).padStart(2,'0')}`;
    const start=api.dayBoundary(a,false,480);
    const end=api.dayBoundary(b,true,480);
    out.push({label,from:start,to:end>frozenEnd?frozenEnd:end});
  }
  return out;
};
async function search(token,selectedAccountId,query,kind,window,start=0,limit=25) {
  const values=kind==='news'?['news']:['social','news','tender','broadcast'];
  const body={requests:[{id:'local_studio',dataSource:'general-index',request:{query:{and:[
    {date:{from:window.from,to:window.to}},
    {meltwaterBoolean:{query,caseSensitive:'no'}},
    {lowLevel:{rune:{type:'all',allQueries:[{type:'any',anyQueries:values.map(value=>({type:'term',field:'metaData.source.informationType',value}))}]}}}
  ]},pagination:{limit,start,sort:{on:'date',order:'desc'}},fetchTotal:true,documentFormat:{consumerAttributes:true,explorePlusAttributes:true}}}]};
  let error;
  for(let attempt=0;attempt<3;attempt++){
    try{
      const r=await fetch(api.msearchUrl(selectedAccountId),{method:'POST',headers:{...api.MSEARCH_HEADERS,authorization:token},body:JSON.stringify(body),signal:AbortSignal.timeout(90000)});
      if(!r.ok) throw new Error(`融文 HTTP ${r.status}`);
      const p=await r.json();
      if(p.error||p.response?.error) throw new Error(`融文检索错误：${JSON.stringify(p.error||p.response.error).slice(0,200)}`);
      const data=p.response??p.requests?.[0]?.response;
      if(!data||typeof data.total!=='number') throw new Error('融文响应缺少总数');
      return data;
    }catch(e){error=e;if(attempt<2)await sleep(1500*(attempt+1));}
  }
  throw error;
}
async function pool(items, concurrency, fn) { const out=new Array(items.length);let next=0;await Promise.all(Array.from({length:concurrency},async()=>{while(next<items.length){const i=next++;out[i]=await fn(items[i],i);}}));return out; }
function summarize(job, docs) {
  const s={...job,records:undefined,months:job.months,returned:docs.length,sentiments:count(docs,x=>x.sentiment),countries:count(docs,x=>x.country),languages:count(docs,x=>x.language),sources:count(docs,x=>x.source).slice(0,25),types:count(docs,x=>x.type)};
  if(job.mode==='full') {
    const byDay=count(docs,x=>new Date(new Date(x.date).getTime()+8*3600000).toISOString().slice(0,10));
    s.days=byDay.sort((a,b)=>b[1]-a[1]).slice(0,30);
  }
  return s;
}
async function run(job) {
  activeJob=job.id; job.state='running'; job.message='连接已登录的 Chrome';
  try {
    const accountKeys=job.accountKey==='pool'?accounts.list().filter(a=>a.accountId).map(a=>a.key):[job.accountKey];
    if(job.accountKey==='pool'&&accountKeys.length<2) throw new Error('并行账号池至少需要两个已连接账号');
    const selectedAccounts=[];
    for(const key of accountKeys) selectedAccounts.push(await accounts.authFor(key));
    job.accountId=selectedAccounts.map(a=>a.accountId);job.accountLabel=selectedAccounts.map(a=>a.label).join('、');
    const windows=monthWindows(job.from,job.to,job.frozenEnd);
    job.months=new Array(windows.length);
    const groups=await pool(windows,Math.min(selectedAccounts.length,3),async(w,i)=>{
      const selected=selectedAccounts[i%selectedAccounts.length];const token=selected.token;
      job.message=`检索 ${w.label}`;
      const first=await search(token,selected.accountId,job.query,job.kind,w,0,25);
      let hits=first.hits||[];
      if(job.mode==='full'&&first.total>25) {
        const offsets=Array.from({length:Math.ceil(first.total/25)-1},(_,i)=>(i+1)*25);
        const pages=await pool(offsets,5,start=>search(token,selected.accountId,job.query,job.kind,w,start,25));
        hits=[...hits,...pages.flatMap(p=>p.hits||[])];
      }
      const rows=hits.map(h=>({...row(h.gyda||h),accountId:selected.accountId}));
      const unique=new Set(rows.map(x=>x.id||`${x.date}|${x.title}|${x.source}`)).size;
      if(job.mode==='full'&&(rows.length!==first.total||unique!==first.total)) throw new Error(`${w.label} 分页校验失败：命中 ${first.total}，取回 ${rows.length}，唯一 ${unique}`);
      job.months[i]={label:w.label,total:first.total,returned:rows.length,accountId:selected.accountId};
      job.total=job.months.filter(Boolean).reduce((n,x)=>n+x.total,0);job.completedMonths=job.months.filter(Boolean).length;
      job.message=`${w.label} 完成：${rows.length} / ${first.total}`;
      return rows;
    });
    const docs=groups.flat();
    job.state='done';job.message='检索完成';job.finishedAt=new Date().toISOString();
    const dir=path.join(runsDir,job.id);fs.mkdirSync(dir,{recursive:true});
    fs.writeFileSync(path.join(dir,'mentions.jsonl'),docs.map(x=>JSON.stringify(x)).join('\n')+'\n');
    const result=summarize(job,docs);fs.writeFileSync(path.join(dir,'result.json'),JSON.stringify(result,null,2));
    job.result=result;
  }catch(e){job.state='error';job.message=e.message;job.finishedAt=new Date().toISOString();}
  finally{activeJob=null;}
}
function getJob(id){const live=jobs.get(id);if(live)return live;const file=path.join(runsDir,id,'result.json');if(/^[a-f0-9]{12}$/.test(id)&&fs.existsSync(file)){const result=JSON.parse(fs.readFileSync(file,'utf8'));return {...result,result};}return null;}
const mime={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'};
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://127.0.0.1');
    if(url.pathname.startsWith('/api/')&&req.method!=='GET'){
      const allowed=[`127.0.0.1:${PORT}`,`localhost:${PORT}`];
      const origin=req.headers.origin;
      if(!allowed.includes(req.headers.host)||origin&&!allowed.some(host=>origin===`http://${host}`))
        return json(res,403,{error:'仅允许本机页面提交请求'});
    }
    if(url.pathname==='/api/status') return json(res,200,{today:today(),accountId:accounts.list()[0].accountId,aiConfigured:!!aiModel,aiModel,running:activeJob});
    if(url.pathname==='/api/accounts'&&req.method==='GET') return json(res,200,{accounts:accounts.list()});
    if(url.pathname==='/api/accounts'&&req.method==='POST') {const p=await readBody(req);return json(res,202,{account:await accounts.add(safe(p.label))});}
    const accountConnect=url.pathname.match(/^\/api\/accounts\/([a-f0-9]{12}|default)\/connect$/);
    if(accountConnect&&req.method==='POST') return json(res,202,{account:await accounts.reconnect(accountConnect[1])});
    const accountEmail=url.pathname.match(/^\/api\/accounts\/([a-f0-9]{12}|default)\/email$/);
    if(accountEmail&&req.method==='POST') {const p=await readBody(req);return json(res,200,{account:accounts.setEmail(accountEmail[1],p.email)});}
    const accountRemove=url.pathname.match(/^\/api\/accounts\/([a-f0-9]{12})$/);
    if(accountRemove&&req.method==='DELETE') return json(res,200,await accounts.remove(accountRemove[1]));
    if(url.pathname==='/api/ai-query'&&req.method==='POST'){
      const input=await readBody(req);if(!aiModel) return json(res,400,{error:'请先在 data/settings.json 设置 aiModel（或设置 AI_MODEL），再启动本地页面。'});
      const subject=safe(input.subject).slice(0,300);if(!subject)return json(res,400,{error:'请输入研究主题'});
      const instruction=`你是融文 Meltwater 检索专家。为用户主题写可直接用于 Meltwater Boolean 的多语种检索式。精确短语用双引号；同义词用 OR；如需消歧，附加一个 AND 语境组。不要把日期、国家或内容类型字段写进布尔式。只返回 JSON 对象，键为 query、languages（字符串数组）、rationale（简短中文）。主题：${subject}。语境：${safe(input.context).slice(0,500)}。语言：${safe(input.languages).slice(0,150)||'中英俄法西德意日韩'}。`;
      const base=aiBaseUrl.replace(/\/$/,'');
      const response=await fetch(`${base}/chat/completions`,{method:'POST',headers:{'content-type':'application/json',...(aiApiKey?{authorization:`Bearer ${aiApiKey}`}:{})},body:JSON.stringify({model:aiModel,messages:[{role:'system',content:'只输出有效 JSON，不使用 Markdown 代码块。'},{role:'user',content:instruction}],temperature:0.2}),signal:AbortSignal.timeout(90000)});
      if(!response.ok) return json(res,502,{error:`AI 服务 HTTP ${response.status}：${(await response.text()).slice(0,200)}`});
      const data=await response.json();const content=String(data.choices?.[0]?.message?.content||'').replace(/^```(?:json)?\s*|\s*```$/g,'').trim();
      let parsed;try{parsed=JSON.parse(content);}catch{parsed={query:content,languages:[],rationale:''};}
      if(!parsed.query||typeof parsed.query!=='string') throw new Error('AI 未返回检索式');
      return json(res,200,{query:parsed.query,languages:parsed.languages||[],rationale:parsed.rationale||''});
    }
    if(url.pathname==='/api/search'&&req.method==='POST'){
      if(activeJob)return json(res,409,{error:'已有检索正在运行，请等待完成。'});
      const p=await readBody(req);const query=safe(p.query).trim(),from=safe(p.from),to=safe(p.to),kind=p.kind==='all'?'all':'news',mode=p.mode==='full'?'full':'preview',accountKey=safe(p.accountKey||'default');
      if(!query||query.length>12000) return json(res,400,{error:'请填写有效检索式'});
      if(!/^\d{4}-\d{2}-\d{2}$/.test(from)||!/^\d{4}-\d{2}-\d{2}$/.test(to)||from>to) return json(res,400,{error:'日期区间无效'});
      if((Date.parse(to)-Date.parse(from))/86400000>369) return json(res,400,{error:'单次检索不能超过370天'});
      if(to>today()) return json(res,400,{error:'结束日期不能晚于今天'});
      if(accountKey!=='pool'&&!accounts.list().some(a=>a.key===accountKey)) return json(res,400,{error:'请选择已添加的融文账号'});
      const id=crypto.randomBytes(6).toString('hex');const job={id,query,from,to,kind,mode,accountKey,frozenEnd:new Date().toISOString(),startedAt:new Date().toISOString(),state:'queued',message:'等待启动',total:0,completedMonths:0,months:[]};
      jobs.set(id,job);void run(job);return json(res,202,{id});
    }
    const match=url.pathname.match(/^\/api\/jobs\/([a-f0-9]{12})(?:\/(mentions|export))?$/);
    if(match){const job=getJob(match[1]);if(!job)return json(res,404,{error:'未找到任务'});if(!match[2])return json(res,200,job.result||{...job,result:undefined});
      const file=path.join(runsDir,match[1],'mentions.jsonl');if(job.state!=='done'||!fs.existsSync(file))return json(res,409,{error:'结果尚未生成'});
      if(match[2]==='export'){res.writeHead(200,{'content-type':'application/x-ndjson; charset=utf-8','content-disposition':`attachment; filename="${match[1]}.jsonl"`});return fs.createReadStream(file).pipe(res);}
      const offset=Math.max(0,Number(url.searchParams.get('offset'))||0),limit=Math.min(100,Math.max(1,Number(url.searchParams.get('limit'))||25));
      const all=fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean);return json(res,200,{total:all.length,offset,rows:all.slice(offset,offset+limit).map(JSON.parse)});
    }
    const requested=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(!['index.html','style.css','accounts.css','app.js'].includes(requested))return json(res,404,{error:'未找到页面'});
    const file=path.join(here,requested);res.writeHead(200,{'content-type':mime[path.extname(file)]||'text/plain','cache-control':'no-store'});return fs.createReadStream(file).pipe(res);
  }catch(e){return json(res,500,{error:e.message||'服务器错误'});}
});
const localUrl=`http://127.0.0.1:${PORT}`;
function openPage(){if(process.platform==='win32'){const child=spawn('rundll32.exe',['url.dll,FileProtocolHandler',localUrl],{windowsHide:true,detached:true,stdio:'ignore'});child.unref();}}
server.listen(PORT,'127.0.0.1',()=>{console.log(`Meltwater 本地研究台：${localUrl}`);if(packaged||process.env.AUTO_OPEN==='1')openPage();});
const ownerPid=Number(process.env.SUYUAN_PARENT_PID||0);
if(ownerPid>0)setInterval(()=>{try{process.kill(ownerPid,0);}catch{server.close(()=>process.exit(0));}},2000).unref();
