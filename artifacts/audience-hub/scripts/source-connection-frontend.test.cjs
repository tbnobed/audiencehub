// Run: node --test scripts/source-connection-frontend.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/sources' });
for (const n of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'MutationObserver', 'CustomEvent', 'Event', 'MouseEvent', 'KeyboardEvent', 'getComputedStyle', 'DocumentFragment']) Object.defineProperty(globalThis, n, { value: dom.window[n], configurable: true });
globalThis.requestAnimationFrame = cb => setTimeout(cb, 0); globalThis.cancelAnimationFrame = clearTimeout;
const React = require('react');
const { render, cleanup, waitFor, fireEvent } = require('@testing-library/react');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
const root = path.resolve(__dirname, '..');
const SECRET = '-----BEGIN PRIVATE KEY-----SECRETVALUE';

function load(server) {
  const cache = new Map(); const requests = [];
  const transport = async (url, o = {}) => {
    const method = o.method || 'GET'; requests.push({ url, method, body: o.body });
    const r = server(new URL(url, 'http://localhost').pathname, method, o.body ? JSON.parse(o.body) : null);
    return new Response(JSON.stringify(r.body), { status: r.status || 200, headers: { 'Content-Type': 'application/json' } });
  };
  function req(file) {
    if (cache.has(file)) return cache.get(file);
    const out = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
    const exports = {}; cache.set(file, exports);
    vm.runInNewContext(out.replaceAll('import.meta.env.DEV', 'false').replaceAll('import.meta.env?.BASE_URL', "'/'"), {
      exports, console, URL, setTimeout: (...a) => { const t = setTimeout(...a); t.unref?.(); return t; }, clearTimeout, fetch: transport, Headers, Response, AbortController, DOMException, window, document, navigator, JSON, Promise, Date, Number, Object, Array, Set,
      require: id => {
        if (id.startsWith('@/') || id.startsWith('.')) {
          const base = id.startsWith('@/') ? path.join(root, 'src', id.slice(2)) : path.resolve(path.dirname(file), id);
          return req([base, `${base}.tsx`, `${base}.ts`].find(p => fs.existsSync(p) && fs.statSync(p).isFile()));
        }
        return require(id);
      },
    }, { filename: file });
    return exports;
  }
  return { C: req(path.join(root, 'src/components/source-connection.tsx')), requests };
}
const src = { id: 5, key: 'zeta', name: 'Zeta', kind: 'sftp_jsonl', priority: 0, record_types: ['contact'], is_active: true, settings: {} };
function mount(server, source = src) {
  const { C, requests } = load(server);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { gcTime: 0 } } });
  clients.push(client);
  const view = render(React.createElement(QueryClientProvider, { client }, React.createElement(C.SourceConnection, { source })));
  return { view, requests, C };
}
const clients = [];
test.afterEach(() => { cleanup(); for (const c of clients.splice(0)) c.clear(); });
const job = (id, status, extra = {}) => ({ job_id: id, status, feed: null, attempts: 1, max_attempts: 5, created_at: '2025-03-01T10:00:00Z', run_after: null, started_at: null, finished_at: null, heartbeat_at: null, counts: { imports_total: 2, imports_completed: 2, rows_total: 1200, rows_ok: 1187, rows_rejected: 13 }, error: null, ...extra });
const defaults = { people: { frequency: 'hourly', timezone: 'America/New_York' } };

test('new connection: requires creds, sends write-only creds once, shows masked state after save', async () => {
  let stored = null; let pullsData = [];
  const { view, requests } = mount((p, m, b) => {
    if (p.endsWith('/pulls')) return { body: { items: pullsData } };
    if (m === 'GET') return { body: stored ? { configured: true, has_credentials: true, config: stored, default_schedules: defaults, recovery_pending: false } : { configured: false, has_credentials: false, config: { mappings: [], schedules: {} }, default_schedules: defaults, recovery_pending: false } };
    if (m === 'PUT') { stored = b.config; return { body: { configured: true, has_credentials: true, config: b.config, default_schedules: defaults, recovery_pending: false } }; }
    if (p.endsWith('/dry-run')) return { body: { limit_per_mapping: 100, items: [{ mapping_id: 'feed_1', rows_checked: 100, fields: ['email', 'zeta_id'], rows_rejected: 3 }] } };
    if (p.endsWith('/pull')) { pullsData = [{ ...job(41, 'queued'), counts: { imports_total: 0, imports_completed: 0, rows_total: 0, rows_ok: 0, rows_rejected: 0 } }]; return { status: 202, body: { job_id: 41, status: 'queued' } }; }
    if (p.endsWith('/pulls')) return { body: { items: pullsData } };
  });
  await waitFor(() => view.getByTestId('empty-mappings'));
  assert.ok(view.getByTestId('button-dry-run').disabled);
  fireEvent.click(view.getByTestId('button-add-mapping'));
  fireEvent.click(view.getByTestId('button-save-connection'));
  await waitFor(() => assert.match(view.getByTestId('list-validation-errors').textContent, /Host is required/));
  assert.equal(requests.filter(r => r.method === 'PUT').length, 0);
  fireEvent.change(view.getByTestId('input-cred-host'), { target: { value: 'sftp.zeta.io' } });
  fireEvent.change(view.getByTestId('input-cred-port'), { target: { value: '22' } });
  fireEvent.change(view.getByTestId('input-cred-username'), { target: { value: 'kin' } });
  fireEvent.change(view.getByTestId('input-cred-private_key'), { target: { value: SECRET } });
  fireEvent.click(view.getByTestId('button-save-connection'));
  await waitFor(() => view.getByTestId('status-credentials-saved'));
  const put = JSON.parse(requests.find(r => r.method === 'PUT').body);
  assert.deepStrictEqual(put.credentials, { host: 'sftp.zeta.io', port: 22, username: 'kin', private_key: SECRET });
  assert.deepStrictEqual(put.config.mappings, [{ id: 'feed_1', feed: 'people', folder: '/users/', pattern: '*.jsonl' }]);
  assert.ok(!view.container.innerHTML.includes('SECRETVALUE'));
  fireEvent.click(view.getByTestId('button-dry-run'));
  await waitFor(() => assert.match(view.getByTestId('row-dry-run-feed_1').textContent, /100 checked.*3 rejected/));
  fireEvent.click(view.getByTestId('button-pull'));
  await waitFor(() => assert.match(view.getByTestId('row-pull-41').textContent, /queued/));
  assert.ok(view.getByTestId('status-pull-polling'));
});

test('edit keeps saved credentials: PUT omits credentials key', async () => {
  const config = { mappings: [{ id: 'people', feed: 'people', folder: '/users/', pattern: '*.jsonl' }], schedules: {} };
  const { view, requests } = mount((p, m, b) => p.endsWith('/pulls') ? { body: { items: [] } } : ({ body: { configured: true, has_credentials: true, config: m === 'PUT' ? b.config : config, default_schedules: defaults, recovery_pending: false } }));
  await waitFor(() => view.getByTestId('status-credentials-saved'));
  fireEvent.change(view.getByTestId('select-schedule-people'), { target: { value: 'daily' } });
  fireEvent.click(view.getByTestId('button-save-connection'));
  await waitFor(() => assert.ok(requests.some(r => r.method === 'PUT')));
  const put = JSON.parse(requests.find(r => r.method === 'PUT').body);
  assert.ok(!('credentials' in put));
  assert.deepStrictEqual(put.config.schedules, { people: { frequency: 'daily', timezone: 'America/New_York', hour: 0 } });
});

test('server errors surface for load, dry-run; recovery pending locks save', async () => {
  const bad = mount(() => ({ status: 403, body: { error: { message: 'Forbidden' } } }));
  await waitFor(() => bad.view.getByTestId('connection-error'));
  cleanup();
  const config = { mappings: [{ id: 'p', feed: 'people', table: 'DB.S.T' }], schedules: {} };
  const { view, requests } = mount((p, m) => p.endsWith('/pulls') ? { body: { items: [job(9, 'failed', { attempts: 5, finished_at: '2025-03-01T11:00:00Z', error: 'Source pull failed. Check configuration and retry to resume committed checkpoints.' })] } } : p.endsWith('/pull') ? { status: 202, body: { job_id: 10, status: 'queued' } } : m === 'GET' ? { body: { configured: true, has_credentials: true, config, default_schedules: {}, recovery_pending: true } } : { status: 422, body: { error: { message: 'Source dry-run failed; verify connectivity' } } }, { ...src, kind: 'snowflake' });
  await waitFor(() => view.getByTestId('status-recovery-pending'));
  assert.ok(view.getByTestId('fieldset-config').disabled, 'config edits locked');
  await waitFor(() => assert.match(view.getByTestId('error-pull-9').textContent, /retry to resume/));
  assert.match(view.getByTestId('counts-pull-9').textContent, /rows 1,200.*ok 1,187.*rejected 13/);
  assert.equal(view.getByTestId('button-pull').disabled, false, 'manual resume allowed');
  fireEvent.click(view.getByTestId('button-pull'));
  await waitFor(() => assert.ok(requests.some(r => r.url.endsWith('/pull') && r.method === 'POST')));
  await waitFor(() => assert.ok(requests.filter(r => r.url.includes('/pulls?limit=50')).length >= 2, 'history invalidated after pull'));
  fireEvent.click(view.getByTestId('button-dry-run'));
  await waitFor(() => assert.match(view.getByTestId('text-dry-run-error').textContent, /dry-run failed/));
});

test('validateConnection mirrors backend rules', () => {
  const { C } = load(() => ({ body: {} }));
  assert.ok(C.validateConnection('snowflake', { mappings: [{ id: 'a', feed: 'people', table: 'T', query: 'SELECT 1' }], schedules: {} }).some(e => /exactly one/.test(e)));
  assert.ok(C.validateConnection('sftp_jsonl', { mappings: [{ id: 'A', feed: 'resources', folder: '/x/' }], schedules: {} }).length >= 3);
  assert.equal(C.validateConnection('zeta_exit', { mappings: [{ id: 'a', feed: 'people', folder: '/users/' }], schedules: {} }).length, 0);
});

test('pull history persists from server and polls every 5s only while active', async () => {
  let calls = 0; let status = 'running';
  const config = { mappings: [{ id: 'p', feed: 'people', folder: '/users/' }], schedules: {} };
  const { view } = mount(p => {
    if (p.endsWith('/pulls')) { calls++; return { body: { items: [job(3, status, { feed: 'people', started_at: '2025-03-01T10:01:00Z', heartbeat_at: '2025-03-01T10:02:00Z' })] } }; }
    return { body: { configured: true, has_credentials: true, config, default_schedules: defaults, recovery_pending: false } };
  });
  await waitFor(() => assert.match(view.getByTestId('status-pull-3').textContent, /running/));
  assert.ok(view.getByTestId('status-pull-polling'));
  status = 'completed';
  await waitFor(() => assert.match(view.getByTestId('status-pull-3').textContent, /completed/), { timeout: 7000 });
  const after = calls;
  await new Promise(r => setTimeout(r, 5500));
  assert.equal(calls, after, 'stops polling when nothing active');
  assert.equal(view.queryByTestId('status-pull-polling'), null);
});
