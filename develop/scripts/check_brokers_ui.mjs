// Run after `npm run build` in frontend. Requires an existing Playwright install.
// PLAYWRIGHT_MODULE may point to its index.mjs; no package installation is performed.
// Optional --runtime performs GET-only checks against localhost:8080, without screenshots.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const dist = resolve(root, 'frontend/dist');
const artifacts = resolve(root, 'develop/reports/brokers-ui');
const server = createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const target = resolve(dist, `.${pathname}`);
  if (target !== dist && !target.startsWith(dist + sep)) {
    response.writeHead(404).end();
    return;
  }
  try {
    const asset = extname(target) ? target : resolve(dist, 'index.html');
    const content = await readFile(asset);
    response.setHeader('Content-Type', { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[extname(asset)] || 'application/octet-stream');
    response.end(content);
  } catch {
    response.writeHead(404).end();
  }
});
let browser;
const result = { status: 'running', started_at: new Date().toISOString(), mock: {}, runtime: null };
await mkdir(artifacts, { recursive: true });
await writeFile(resolve(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
try {
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  await new Promise((done, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', done);
  });
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', () => errors.push('pageerror'));
  let brokers = [];
  let saves = 0;
  const unexpected = [];
  const adapter = { adapter_code: 'MOCK_SANDBOX', provider_code: 'MOCK', environment_code: 'SANDBOX', fields: [{ name: 'endpoint', required: true, default_value: 'sandbox.example' }] };
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== `http://127.0.0.1:${server.address().port}`) {
      unexpected.push('external-request');
      return route.abort();
    }
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.continue();
    let body;
    let status = 200;
    if (path === '/api/brokers' && request.method() === 'GET') body = { brokers, adapters: [adapter] };
    else if (path === '/api/brokers' && request.method() === 'POST') {
      saves += 1;
      const draft = request.postDataJSON();
      if (saves === 1) {
        status = 422;
        body = { detail: { code: 'VALIDATION_ERROR', message: 'Проверьте настройки брокера.', fields: [{ path: 'display_name', code: 'REQUIRED', message: 'Укажите название.' }] } };
      } else {
        assert.equal(draft.display_name, 'UI smoke sandbox');
        assert.equal(draft.adapter_code, adapter.adapter_code);
        body = { ...draft, id: 'synthetic-broker', created_at: '2026-09-09T10:00:00Z', updated_at: '2026-09-09T10:00:00Z' };
        brokers = [body];
      }
    } else if (path === '/api/brokers/synthetic-broker/accounts') body = { accounts: [{ broker_id: 'synthetic-broker', broker_name: 'UI smoke sandbox', account_id: 'synthetic-account', name: 'Учебный счёт', status: 'OPEN', account_type: 'SANDBOX', total_amount: null, free_cash: null }], errors: [], total_amounts: [], total_free_cash: [] };
    else if (path === '/api/health') body = { status: 'ok', service: 'backend', version: 'smoke', database: 'ok', schema: 'compatible' };
    else if (path === '/api/trading-sessions/status') body = { status: 'NO_ACTIVE', total: 0, open: 0, closed: 0, unavailable: 0 };
    else {
      unexpected.push(`${request.method()} ${path}`);
      return route.fulfill({ status: 404, json: {} });
    }
    return route.fulfill({ status, json: body });
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/brokers`);
  await page.getByText('Подключённые брокеры отсутствуют').waitFor();
  await page.getByRole('button', { name: 'Добавить брокера' }).click();
  await page.getByLabel(/^Адаптер/).selectOption(adapter.adapter_code);
  assert.equal(await page.getByLabel('endpoint', { exact: true }).inputValue(), 'sandbox.example');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await page.getByText('Укажите название.').waitFor();
  assert.equal(await page.getByLabel(/^Название/).getAttribute('aria-invalid'), 'true');
  await page.getByLabel(/^Название/).fill('UI smoke sandbox');
  await page.screenshot({ path: resolve(artifacts, 'mock-create-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  const row = page.locator('article.broker-row');
  await row.waitFor();
  assert.equal(await page.locator('form').count(), 0);
  await row.click();
  assert.match(await row.getAttribute('class'), /data-table__row--selected/);
  await row.dblclick();
  await page.getByRole('heading', { name: 'Редактирование интеграции' }).waitFor();
  assert.equal(await page.getByLabel(/^Адаптер/).isDisabled(), true);
  await page.getByRole('button', { name: 'Отмена' }).click();
  await page.screenshot({ path: resolve(artifacts, 'mock-list-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'Счета', exact: true }).click();
  await page.getByText('Учебный счёт', { exact: true }).waitFor();
  assert.equal(new URL(page.url()).pathname, '/brokers/synthetic-broker/accounts');
  await page.screenshot({ path: resolve(artifacts, 'mock-accounts-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: 'Брокеры', exact: true }).click();
  await row.waitFor();
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.screenshot({ path: resolve(artifacts, `mock-list-mobile-${width}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `list horizontal overflow at ${width}px`);
    await page.getByRole('button', { name: 'Добавить брокера' }).click();
    await page.getByLabel(/^Адаптер/).selectOption(adapter.adapter_code);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `form horizontal overflow at ${width}px`);
    await page.getByRole('button', { name: 'Отмена' }).click();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  result.mock = { passed: true, create_requests_intercepted: saves, checks: ['empty-list', 'create-form', 'adapter-fields', 'validation-recovery', 'save-and-refresh', 'select-edit-cancel', 'accounts-navigation', 'return-navigation', 'mobile-width', 'no-page-errors'] };
  await context.close();

  if (process.argv.includes('--runtime')) {
    const live = await browser.newContext({ serviceWorkers: 'block' });
    const page = await live.newPage();
    let denied = 0;
    let failures = 0;
    page.on('pageerror', () => { failures += 1; });
    await live.route('**/*', (route) => {
      const request = route.request();
      if (new URL(request.url()).origin !== 'http://localhost:8080' || !['GET', 'HEAD'].includes(request.method())) {
        denied += 1;
        return route.abort();
      }
      return route.continue();
    });
    await page.goto('http://localhost:8080/brokers');
    await page.locator('article.broker-row, .empty, .page .error').first().waitFor();
    assert.equal(await page.locator('.page .error').count(), 0, 'runtime broker page error');
    const brokerCount = await page.locator('article.broker-row').count();
    await page.getByRole('button', { name: 'Добавить брокера' }).click();
    await page.getByRole('heading', { name: 'Новая интеграция' }).waitFor();
    await page.getByRole('button', { name: 'Отмена' }).click();
    let accountCount = null;
    if (brokerCount) {
      await page.getByRole('button', { name: 'Счета', exact: true }).first().click();
      await page.locator('.page article, .page .empty, .page .error').first().waitFor();
      assert.equal(await page.locator('.page .error').count(), 0, 'runtime accounts page error');
      accountCount = await page.locator('.page article').count();
      await page.getByRole('link', { name: 'Брокеры', exact: true }).click();
      await page.locator('article.broker-row').first().waitFor();
    }
    assert.equal(denied, 0);
    assert.equal(failures, 0);
    result.runtime = { passed: true, broker_count: brokerCount, account_count_first_broker: accountCount, blocked_requests: denied, page_errors: failures, mode: 'GET-only; no screenshots or response bodies' };
    await live.close();
  }
  result.status = 'passed';
  result.finished_at = new Date().toISOString();
  await writeFile(resolve(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
} catch (error) {
  result.status = 'failed';
  result.finished_at = new Date().toISOString();
  result.error_type = error instanceof assert.AssertionError ? 'AssertionError' : 'Error';
  await writeFile(resolve(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
  console.error(JSON.stringify(result));
  process.exitCode = 1;
} finally {
  await browser?.close();
  await new Promise((done) => server.close(done));
}
