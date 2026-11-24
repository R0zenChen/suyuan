// Zero-dependency Chrome DevTools Protocol client (Node >= 22, global WebSocket).
//
// Discovers the browser-level WebSocket endpoint from Chrome's DevToolsActivePort
// file. This is the Chrome 144+ path: the user enables
// "Allow remote debugging for this browser instance" at
// chrome://inspect/#remote-debugging, Chrome opens a local port and writes the
// endpoint to DevToolsActivePort. In this mode /json/version and /json/list
// return 404 -- only the WebSocket works.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export function chromeUserDataDir() {
  if (process.env.CHROME_USER_DATA_DIR) return process.env.CHROME_USER_DATA_DIR;
  if (process.platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'User Data');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Google', 'Chrome');
  }
  const google = path.join(os.homedir(), '.config', 'google-chrome');
  return fs.existsSync(google) ? google : path.join(os.homedir(), '.config', 'chromium');
}

/** Returns ws://host:port/devtools/browser/<id> or null when Chrome is not exposing one. */
export function readDevToolsEndpoint(userDataDir = chromeUserDataDir()) {
  const file = path.join(userDataDir, 'DevToolsActivePort');
  if (!fs.existsSync(file)) return null;
  const [port, wsPath] = fs.readFileSync(file, 'utf8').trim().split(/\r?\n/);
  if (!port || !wsPath) return null;
  return `ws://127.0.0.1:${port}${wsPath}`;
}

export class CDP {
  constructor(ws) {
    this.ws = ws;
    this._id = 0;
    this.pending = new Map();
    this.listeners = new Map();
  }

  /**
   * Connect with a handshake timeout and retries.
   * The browser-level endpoint accepts one client at a time: if a previous
   * connection is still open, the new WebSocket handshake hangs instead of
   * failing fast, so a bare connect() can block for minutes.
   */
  static async connect(url, { timeoutMs = 15000, retries = 3 } = {}) {
    let lastErr;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const ws = new WebSocket(url);
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            try { ws.close(); } catch { /* already closing */ }
            reject(new Error('CDP websocket handshake timeout after ' + timeoutMs + 'ms'));
          }, timeoutMs);
          ws.onopen = () => { clearTimeout(timer); resolve(); };
          ws.onerror = (e) => { clearTimeout(timer); reject(new Error('CDP websocket error: ' + (e?.message || 'unknown'))); };
        });
        const cdp = new CDP(ws);
        ws.onmessage = (ev) => cdp._onMessage(String(ev.data));
        return cdp;
      } catch (e) {
        lastErr = e;
        if (attempt < retries) await sleep(1500 * (attempt + 1));
      }
    }
    throw new Error(`Could not connect to ${url} after ${retries + 1} attempts: ${lastErr?.message}`);
  }

  _onMessage(data) {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    if (msg.id !== undefined && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.method || ''} ${msg.error.message} (${msg.error.code})`));
      else resolve(msg.result);
      return;
    }
    if (msg.method) {
      for (const key of [`${msg.sessionId || ''}|${msg.method}`, `*|${msg.method}`]) {
        const list = this.listeners.get(key);
        if (list) for (const fn of [...list]) {
          try { fn(msg.params, msg.sessionId); } catch (e) { console.error('listener error:', e.message); }
        }
      }
    }
  }

  /** Register a handler. Pass sessionId to scope it, or omit for browser-level events. */
  on(method, fn, sessionId) {
    const key = `${sessionId || ''}|${method}`;
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(fn);
    return () => {
      const list = this.listeners.get(key) || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    };
  }

  send(method, params = {}, sessionId, timeoutMs = 120000) {
    const id = ++this._id;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout after ${timeoutMs}ms: ${method}`));
        }
      }, timeoutMs);
    });
  }

  async listTargets() {
    return (await this.send('Target.getTargets')).targetInfos;
  }

  async attachToTarget(targetId) {
    const { sessionId } = await this.send('Target.attachToTarget', { targetId, flatten: true });
    return sessionId;
  }

  async evaluate(sessionId, expression, { awaitPromise = true, returnByValue = true } = {}) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue, userGesture: true, allowUnsafeEvalBlockedByCSP: true
    }, sessionId);
    if (r.exceptionDetails) {
      const e = r.exceptionDetails;
      throw new Error('page JS error: ' + (e.exception?.description || e.text || JSON.stringify(e)));
    }
    return r.result?.value;
  }

  close() { try { this.ws.close(); } catch { /* already closed */ } }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
