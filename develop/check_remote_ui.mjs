// Read-only browser acceptance. Only login and logout may modify the session.
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, chmod, readFile, mkdir, writeFile, rm } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const origin = 'https://135.136.178.252';
const output = resolve(dirname(fileURLToPath(import.meta.url)), 'reports/vps-sandbox-20261001');
const result = { status: 'running', started_at: new Date().toISOString(), console_errors: 0, page_errors: 0, network_errors: [], blocked_requests: 0 };
let chrome, socket, profile, phase = 'startup', sequence = 0, authenticated = false;
const pending = new Map();
const requests = new Map();
const responseStatuses = new Map();
const requestMethods = new Map();
const safeNetworkErrors = new Set(['net::ERR_ABORTED', 'net::ERR_FAILED', 'net::ERR_CONNECTION_REFUSED', 'net::ERR_CONNECTION_RESET', 'net::ERR_CONNECTION_CLOSED', 'net::ERR_TIMED_OUT', 'net::ERR_NAME_NOT_RESOLVED', 'net::ERR_CERT_AUTHORITY_INVALID', 'net::ERR_CERT_DATE_INVALID', 'net::ERR_INTERNET_DISCONNECTED', 'net::ERR_BLOCKED_BY_CLIENT']);
const delay = ms => new Promise(done => setTimeout(done, ms));
const deadline = Date.now() + 150_000;
function command(method, params = {}) {
  return new Promise((done, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('cdp_timeout')); }, 12_000);
    pending.set(id, { done, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, awaitPromise = false) {
  const value = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  if (value.exceptionDetails) throw new Error('evaluation_failed');
  return value.result.value;
}
async function waitFor(expression, timeout = 25_000) {
  const until = Math.min(deadline, Date.now() + timeout);
  while (Date.now() < until) {
    const value = await evaluate(expression);
    if (value) return value;
    await delay(250);
  }
  throw new Error('dom_timeout');
}
function expectedLogoutAbort(error, report) {
  return error.path === '/api/auth/logout' && error.method === 'POST' && error.response_status === 204
    && error.error_code === 'net::ERR_ABORTED' && error.canceled === true
    && report.logout === 'passed' && report.post_logout_session_status === 401;
}
const safePath = value => { try { return new URL(value).pathname; } catch { return 'unknown'; } };
try {
  await mkdir(output, { recursive: true });
  const { stdout } = await promisify(execFile)('ssh', ['-i', '/Users/artemivanov/.ssh/id_rsa', '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', 'codex-deploy@135.136.178.252', 'sudo -n cat /root/moex-sentinel-operator-password'], { timeout: 30_000, maxBuffer: 4096 });
  let password = stdout.trim();
  if (!password) throw new Error('empty_password');
  profile = await mkdtemp('/private/tmp/moex-browser-');
  await chmod(profile, 0o700);
  chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', ['--headless=new', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--window-size=1440,1000', 'about:blank'], { stdio: 'ignore' });
  chrome.on('error', () => {});
  let port;
  for (let attempt = 0; attempt < 80; attempt++) {
    try { port = Number((await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; } catch { await delay(100); }
  }
  if (!port) throw new Error('chrome_start_failed');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(5000) })).json();
  socket = new WebSocket(targets.find(item => item.type === 'page').webSocketDebuggerUrl);
  await new Promise((done, reject) => {
    const timer = setTimeout(() => reject(new Error('socket_open_timeout')), 12_000);
    socket.addEventListener('open', () => { clearTimeout(timer); done(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('socket_failed')); }, { once: true });
  });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id);
      if (!item) return;
      clearTimeout(item.timer); pending.delete(message.id);
      if (message.error) item.reject(new Error('cdp_failed')); else item.done(message.result);
      return;
    }
    const p = message.params;
    if (message.method === 'Runtime.consoleAPICalled' && p.type === 'error') result.console_errors++;
    if (message.method === 'Runtime.exceptionThrown') result.page_errors++;
    if (message.method === 'Network.requestWillBeSent') {
      requests.set(p.requestId, safePath(p.request.url)); requestMethods.set(p.requestId, p.request.method);
    }
    if (message.method === 'Network.loadingFailed') result.network_errors.push({
      path: requests.get(p.requestId) || 'unknown', method: requestMethods.get(p.requestId) || 'unknown', status: 'failed',
      error_code: safeNetworkErrors.has(p.errorText) ? p.errorText : 'unclassified',
      canceled: p.canceled === true, response_status: responseStatuses.get(p.requestId) ?? null,
    });
    if (message.method === 'Network.responseReceived') {
      responseStatuses.set(p.requestId, p.response.status);
      if (safePath(p.response.url) === '/api/auth/logout') result.logout_response_status = p.response.status;
    }
    if (message.method === 'Network.responseReceived' && p.response.status >= 400 && !(['login', 'verify-auth'].includes(phase) && p.response.status === 401 && safePath(p.response.url) === '/api/auth/session')) result.network_errors.push({ path: safePath(p.response.url), status: p.response.status });
    if (message.method === 'Fetch.requestPaused') {
      const request = p.request;
      const allowed = request.url.startsWith(origin + '/') && (['GET', 'HEAD'].includes(request.method) || (request.method === 'POST' && ((phase === 'login' && safePath(request.url) === '/api/auth/login') || (phase === 'logout' && safePath(request.url) === '/api/auth/logout'))));
      if (!allowed) result.blocked_requests++;
      command(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', allowed ? { requestId: p.requestId } : { requestId: p.requestId, errorReason: 'BlockedByClient' }).catch(() => {});
    }
  });
  await command('Page.enable'); await command('Runtime.enable'); await command('Network.enable');
  await command('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
  phase = 'login';
  await command('Page.navigate', { url: origin + '/instruments' });
  await waitFor(`!!document.querySelector('input[name="password"]')`);
  await evaluate(`(() => { for (const [name,value] of [['username','operator'],['password',${JSON.stringify(password)}]]) { const input=document.querySelector('input[name="'+name+'"]'); input.value=value; input.dispatchEvent(new Event('input',{bubbles:true})); } document.querySelector('button[type="submit"]').click(); return true; })()`);
  password = '';
  await waitFor(`location.pathname === '/instruments' && document.querySelectorAll('.data-table tbody tr').length > 0`);
  authenticated = true;
  phase = 'instruments';
  result.instruments = await evaluate(`({ row_count:document.querySelectorAll('.data-table tbody tr').length, sample_tickers:[...document.querySelectorAll('.data-table tbody tr td:first-child')].slice(0,5).map(e=>e.textContent.trim()), errors:document.querySelectorAll('.page .error').length })`);
  if (result.instruments.errors) throw new Error('instrument_errors');
  const screenshot = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(resolve(output, 'instruments.png'), Buffer.from(screenshot.data, 'base64'), { mode: 0o600 });
  await chmod(resolve(output, 'instruments.png'), 0o600);
  await evaluate(`(() => { const input=document.querySelector('.instrument-search'); input.value='SBER'; input.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  await waitFor(`[...document.querySelectorAll('.data-table tbody tr')].some(row => row.querySelector('td strong')?.textContent.trim() === 'SBER')`);
  result.instruments.filtered_row_count = await evaluate(`document.querySelectorAll('.data-table tbody tr').length`);
  result.instruments.sber_search = true;
  await evaluate(`(() => { [...document.querySelectorAll('.data-table tbody tr')].find(row => row.querySelector('td strong')?.textContent.trim() === 'SBER').dispatchEvent(new MouseEvent('dblclick',{bubbles:true})); return true; })()`);
  await waitFor(`location.pathname.startsWith('/instruments/') && [...document.querySelectorAll('h2')].some(e=>e.textContent.startsWith('SBER —'))`);
  result.instruments.sber_details = true;
  phase = 'positions';
  await command('Page.navigate', { url: origin + '/positions' });
  await waitFor(`document.querySelector('table.data-table')?.querySelectorAll('tbody tr').length === 6`);
  // Operations have another table; the first table contains open automata.
  await delay(1000);
  result.positions = await evaluate(`({ rows:[...document.querySelector('table.data-table').querySelectorAll('tbody tr')].map(row=>({ticker:row.cells[0].textContent.trim(),quantity_lots:Number(row.cells[4].textContent.trim())})), errors:document.querySelectorAll('.page .error').length })`);
  if (result.positions.rows.length !== 6 || result.positions.rows.some(row => !Number.isFinite(row.quantity_lots) || row.quantity_lots <= 0) || result.positions.errors) throw new Error('positions_failed');
  if (result.page_errors || result.console_errors || result.network_errors.length || result.blocked_requests) throw new Error('browser_errors');
  result.status = 'passed';
} catch {
  result.status = 'failed'; result.failure_phase = phase; process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try {
      authenticated ||= await evaluate(`!![...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Выйти')`);
      if (authenticated) {
        phase = 'logout';
        await evaluate(`(() => { const button=[...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Выйти'); if (!button) throw new Error('missing_logout'); button.click(); return true; })()`);
        await waitFor(`location.pathname === '/login' && !!document.querySelector('input[name="password"]')`, 12_000);
        result.logout = 'passed';
        phase = 'verify-auth';
        result.post_logout_session_status = await evaluate(`fetch('/api/auth/session', {cache:'no-store'}).then(response => response.status)`, true);
        if (result.post_logout_session_status !== 401) throw new Error('session_still_active');
      }
    } catch { result.logout = 'failed'; result.status = 'failed'; process.exitCode = 1; }
  }
  result.expected_network_events = result.network_errors.filter(error => expectedLogoutAbort(error, result));
  result.network_errors = result.network_errors.filter(error => !expectedLogoutAbort(error, result));
  if (result.page_errors || result.console_errors || result.network_errors.length || result.blocked_requests) {
    result.status = 'failed'; process.exitCode = 1;
  }
  socket?.close();
  for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('shutdown')); }
  if (chrome && chrome.exitCode === null) {
    chrome.kill('SIGTERM');
    await Promise.race([new Promise(done => chrome.once('exit', done)), delay(3000)]);
    if (chrome.exitCode === null) { chrome.kill('SIGKILL'); await Promise.race([new Promise(done => chrome.once('exit', done)), delay(2000)]); }
  }
  if (profile) await rm(profile, { recursive: true, force: true });
  result.finished_at = new Date().toISOString();
  await mkdir(output, { recursive: true });
  await writeFile(resolve(output, 'browser.json'), JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
  await chmod(resolve(output, 'browser.json'), 0o600);
  console.log(JSON.stringify(result));
}
