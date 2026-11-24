import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const platformChrome = process.platform === 'win32'
  ? ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe']
  : process.platform === 'darwin'
    ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', path.join(os.homedir(), 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    : ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
const chromeCandidates = [process.env.CHROME_EXECUTABLE, ...platformChrome].filter(Boolean);
const chromeExe = () => chromeCandidates.find(candidate => fs.existsSync(candidate) ||
  (!candidate.includes(path.sep) && spawnSync(process.platform === 'win32' ? 'where.exe' : 'which', [candidate], {stdio:'ignore'}).status === 0));
const validEmail = value => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || ''));
export function emailFromToken(token) {
  try {
    const claims = JSON.parse(Buffer.from(String(token).replace(/^Bearer\s+/i, '').split('.')[1], 'base64url').toString('utf8'));
    const candidates = [claims.email, claims.email_address, claims.preferred_username, claims.upn,
      claims.user?.email, claims.profile?.email, claims.sub];
    return candidates.find(validEmail) || '';
  } catch { return ''; }
}
export function accountIdFromToken(token) {
  try {
    const claims = JSON.parse(Buffer.from(String(token).replace(/^Bearer\s+/i, '').split('.')[1], 'base64url').toString('utf8'));
    const value = claims.company?._id || claims.user?.activeCompanyId || '';
    return /^[a-f0-9]{24}$/i.test(value) ? value : '';
  } catch { return ''; }
}

export function createAccountStore({ api, cdpModule, metadataFile, defaultAccountId, profileRoot = path.join(os.homedir(), 'AppData', 'Local', 'MeltwaterResearchStudio', 'Profiles') }) {
  fs.mkdirSync(path.dirname(metadataFile), { recursive: true });
  const saved = fs.existsSync(metadataFile) ? JSON.parse(fs.readFileSync(metadataFile, 'utf8')) : [];
  const accounts = new Map();
  const savedDefault = saved.find(item => item.key === 'default') || {};
  accounts.set('default', { key:'default', label:'当前 Chrome 账号', accountId:defaultAccountId || savedDefault.accountId || '', email:savedDefault.email || '', kind:'existing', status:'可连接', token:null, exp:0 });
  for (const item of saved) {
    if (!/^[a-f0-9]{12}$/.test(item.key)) continue;
    const dir=path.join(profileRoot,item.key);
    accounts.set(item.key,{key:item.key,label:String(item.label||'融文账号'),accountId:String(item.accountId||''),email:String(item.email||''),kind:'profile',profileDir:dir,status:'待连接',token:null,exp:0});
  }
  const tasks = new Map();
  const persist = () => fs.writeFileSync(metadataFile, JSON.stringify([...accounts.values()].map(a=>({key:a.key,label:a.label,accountId:a.accountId,email:a.email||'',kind:a.kind})),null,2));
  const publicAccount = a => ({key:a.key,label:a.label,accountId:a.accountId,email:a.email||'',status:a.status,kind:a.kind,expiresAt:a.exp?new Date(a.exp*1000).toISOString():null});
  const list = () => [...accounts.values()].map(publicAccount);

  async function connectProfile(a) {
    if (tasks.has(a.key)) return tasks.get(a.key);
    const task=(async()=>{
      const exe=chromeExe();
      if(!exe) throw new Error('未找到 Chrome。请设置 CHROME_EXECUTABLE。');
      fs.mkdirSync(a.profileDir,{recursive:true});
      a.status='正在打开融文登录页';
      const child=spawn(exe,[`--user-data-dir=${a.profileDir}`,'--remote-debugging-port=0','--no-first-run','--no-default-browser-check','--new-window','https://app.meltwater.com/'],{detached:true,stdio:'ignore',windowsHide:false});
      a.chromeChild=child;
      child.unref();
      let cdp=null;
      for(let i=0;i<45 && !a.deleted;i++){
        const endpoint=cdpModule.readDevToolsEndpoint(a.profileDir);
        if(endpoint){try{cdp=await cdpModule.CDP.connect(endpoint,{timeoutMs:1800,retries:0});break;}catch{}}
        await sleep(400);
      }
      if(!cdp) throw new Error('Chrome 调试端点未就绪，请确认登录窗口已打开。');
      a.cdp=cdp;
      let attached=null, targetId=null, token=null, accountId=null, lastReload=0;
      try{
        const deadline=Date.now()+10*60*1000;
        while(Date.now()<deadline && !a.deleted){
          const targets=await cdp.listTargets();
          const page=targets.find(t=>t.type==='page'&&/meltwater\.com/.test(t.url));
          if(page && page.targetId!==targetId){
            if(attached){try{await cdp.send('Target.detachFromTarget',{sessionId:attached});}catch{}}
            targetId=page.targetId;a.loginTargetId=targetId;attached=await cdp.attachToTarget(targetId);lastReload=Date.now();
            await cdp.send('Page.enable',{},attached);await cdp.send('Network.enable',{},attached);
            cdp.on('Network.requestWillBeSent',p=>{
              const h=p.request?.headers||{};const bearer=h.authorization||h.Authorization;
              if(bearer?.startsWith('Bearer ')&&/meltwater\.io/.test(p.request?.url||''))token=bearer;
              const m=(p.request?.url||'').match(/\/accounts\/([a-f0-9]+)\//i);
              if(m)accountId=m[1];
            },attached);
            a.status='请在融文官方页面完成登录';
          }
          if(token&&(accountId||accountIdFromToken(token)) && !a.deleted){
            a.token=token;a.exp=api.tokenExpirySeconds(token)||Math.floor(Date.now()/1000)+3600;
            a.accountId=accountId||accountIdFromToken(token);a.email=emailFromToken(token)||a.email;a.status='已连接';persist();
            try{await cdp.send('Target.closeTarget',{targetId});}catch{}
            return publicAccount(a);
          }
          if(attached&&page?.url?.startsWith('https://app.meltwater.com/')&&Date.now()-lastReload>18000){
            lastReload=Date.now();try{await cdp.send('Page.reload',{},attached);}catch{}
          }
          await sleep(800);
        }
        throw new Error('等待登录超时。请重新点击连接账号。');
      }finally{cdp.close();a.cdp=null;a.loginTargetId=null;}
    })();
    tasks.set(a.key,task);
    try{return await task;}catch(e){if(!a.deleted)a.status=`连接失败：${e.message}`;throw e;}finally{tasks.delete(a.key);a.chromeChild=null;}
  }

  async function add(label) {
    if (accounts.size>=8) throw new Error('最多添加7个独立账号。');
    const key=crypto.randomBytes(6).toString('hex');
    const a={key,label:String(label||`融文账号 ${accounts.size}`).slice(0,60),accountId:'',email:'',kind:'profile',profileDir:path.join(profileRoot,key),status:'等待打开',token:null,exp:0};
    accounts.set(key,a);persist();
    void connectProfile(a).catch(()=>{});
    return publicAccount(a);
  }
  async function reconnect(key) {
    const a=accounts.get(key);if(!a)throw new Error('账号不存在');
    if(a.kind==='existing'){
      const s=await api.connectAndAuth();
      try{a.token=s.token;a.exp=api.tokenExpirySeconds(s.token)||Math.floor(Date.now()/1000)+3600;a.accountId=s.accountId||accountIdFromToken(s.token)||a.accountId;a.email=emailFromToken(s.token)||a.email;a.status='已连接';persist();return publicAccount(a);}finally{s.cdp.close();}
    }
    void connectProfile(a).catch(()=>{});
    return publicAccount(a);
  }
  async function authFor(key) {
    const a=accounts.get(key);if(!a)throw new Error('账号不存在');
    if(a.token&&a.accountId&&Date.now()/1000<a.exp-300)return {token:a.token,accountId:a.accountId,label:a.label,key:a.key};
    if(a.kind==='existing')await reconnect(key);else await connectProfile(a);
    if(!a.accountId)throw new Error('未能从融文请求识别账号 ID，请打开融文搜索页后重试连接。');
    return {token:a.token,accountId:a.accountId,label:a.label,key:a.key};
  }
  function setEmail(key, email) {
    const a=accounts.get(key);if(!a)throw new Error('账号不存在');
    const value=String(email||'').trim();if(value&&!validEmail(value))throw new Error('邮箱格式无效');
    a.email=value;persist();return publicAccount(a);
  }
  async function remove(key) {
    const a=accounts.get(key);if(!a)throw new Error('账号不存在');
    if(a.kind==='existing')throw new Error('当前 Chrome 账号不能删除；可以清空邮箱或删除独立账号。');
    a.deleted=true;
    if(a.cdp&&a.loginTargetId){try{await a.cdp.send('Target.closeTarget',{targetId:a.loginTargetId});}catch{}}
    if(a.chromeChild){try{a.chromeChild.kill();}catch{}}
    if(process.platform==='win32'&&a.chromeChild?.pid)spawnSync('taskkill.exe',['/PID',String(a.chromeChild.pid),'/T','/F'],{windowsHide:true,timeout:3000});
    const task=tasks.get(key);
    if(task)await Promise.race([task.catch(()=>{}),sleep(2500)]);
    const dir=path.resolve(a.profileDir),root=path.resolve(profileRoot);
    if(!dir.startsWith(root+path.sep)||path.basename(dir)!==key)throw new Error('账号目录无效');
    try{fs.rmSync(dir,{recursive:true,force:true,maxRetries:5,retryDelay:400});}
    catch(e){a.deleted=false;throw new Error(`账号浏览器仍占用数据目录，请关闭该登录窗口后重试删除：${e.message}`);}
    accounts.delete(key);persist();return {removed:true,key};
  }
  return {list,add,reconnect,authFor,setEmail,remove};
}
