// Run: node --test scripts/segments-frontend.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/segments' });
for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'MutationObserver', 'CustomEvent', 'Event', 'MouseEvent', 'KeyboardEvent', 'getComputedStyle', 'DocumentFragment']) {
  Object.defineProperty(globalThis, name, { value: dom.window[name], configurable: true });
}
globalThis.requestAnimationFrame = cb => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = clearTimeout;
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.HTMLElement.prototype.scrollIntoView = function () {};
const React = require('react');
const { render, cleanup, waitFor, fireEvent, act } = require('@testing-library/react');
const { MemoryRouter, Routes, Route } = require('react-router-dom');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
const root = path.resolve(__dirname, '..');

const fields = [
  { key: 'trait.donor_status', type: 'trait', field: 'donor_status', label: 'Partner status', group: 'Engagement', data_type: 'enum', operators: ['in', 'not_in'], enum_values: ['lapsing', 'lapsed', 'active'] },
  { key: 'trait.ltv_total', type: 'trait', field: 'ltv_total', label: 'Lifetime giving', group: 'Giving', data_type: 'number', operators: ['eq', 'gte', 'between'], enum_values: [] },
  { key: 'gift.count', type: 'gift', field: 'count', aggregate: 'count', label: 'Gift count', group: 'Giving', data_type: 'number', operators: ['gte'], enum_values: [] },
];
const definition = { version: 1, root: { op: 'and', rules: [{ type: 'trait', field: 'ltv_total', operator: 'gte', value: 500 }] } };
const seg = { id: 7, name: 'Lapsed, LTV 500+', description: 'Reactivation', definition, status: 'active', refresh_schedule: 'daily', last_count: 12340, last_materialized_at: '2025-01-02T04:00:00Z', created_by: 3, owner: 'Rae Analyst', updated_at: '2025-01-02T04:00:00Z', activation_count: 2, change: -120 };

function load(role) {
  const cache = new Map();
  const requests = [];
  const transport = async (url, options = {}) => {
    requests.push({ url, method: options.method || 'GET', body: options.body });
    const u = new URL(url, 'http://localhost');
    const json = b => new Response(JSON.stringify(b));
    if (u.pathname === '/api/segments/fields') return json({ fields, gift_values: { fund: ['Missions'] } });
    if (u.pathname === '/api/segments' && (options.method || 'GET') === 'GET') return json({ items: [seg] });
    if (u.pathname === '/api/segments' && options.method === 'POST') return json({ ...seg, ...JSON.parse(options.body), id: 8 });
    if (u.pathname === '/api/segments/7' && options.method === 'PUT') return json({ ...seg, ...JSON.parse(options.body) });
    if (u.pathname === '/api/segments/7') return json(seg);
    if (u.pathname === '/api/segments/7/count-history') return json({ items: [{ counted_at: '2025-01-01T04:00:00Z', count: 12460, added: 10, removed: 0 }, { counted_at: '2025-01-02T04:00:00Z', count: 12340, added: 30, removed: 150 }] });
    if (u.pathname === '/api/segments/7/activity') return json({ audit: [{ action: 'segment.update', created_at: '2025-01-02T03:00:00Z', user_id: 3 }], activations: [{ id: 4, name: 'Meta reactivation', status: 'active' }] });
    if (u.pathname === '/api/segments/7/members') return json({ items: [{ id: 101, first_name: 'Ana', last_name: 'Ruiz', email: 'a***@x.org', phone: null, donor_status: 'lapsed', source_keys: ['donor_crm'] }], next_cursor: null });
    if (u.pathname === '/api/segments/7/count-history/export') return json({ filename: 'segment-7-count-history.csv', csv: 'counted_at,count,added,removed\n' });
    if (u.pathname === '/api/segments/preview') return json({ count: 4821, took_ms: 41.2 });
    if (u.pathname === '/api/segments/sample') return json({ items: [], breakdowns: { status: [{ key: 'lapsed', count: 20 }], source: [{ key: 'donor_crm', count: 25 }] } });
    if (u.pathname === '/api/client-errors') return json({});
    throw new Error(`unexpected request ${url}`);
  };
  const auth = { useAuth: () => ({ user: { id: 3, name: 'T', role, email: '', csrf_token: '', auth_mode: '', app_env: 'dev' }, isLoading: false }) };
  function req(filename) {
    if (cache.has(filename)) return cache.get(filename);
    const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
    const exports = {};
    cache.set(filename, exports);
    vm.runInNewContext(output.replaceAll('import.meta.env.DEV', 'false'), {
      exports, console, Blob, URL: TestURL, URLSearchParams, setTimeout: unrefTimeout, clearTimeout, fetch: transport, Headers, Response, AbortController, DOMException,
      window, document, navigator, HTMLElement, getComputedStyle, requestAnimationFrame, cancelAnimationFrame, JSON, Promise, Date,
      require: id => {
        if (id === '@/hooks/use-auth') return auth;
        if (id.startsWith('@/') || id.startsWith('.')) {
          const base = id.startsWith('@/') ? path.join(root, 'src', id.slice(2)) : path.resolve(path.dirname(filename), id);
          const candidate = [base, `${base}.tsx`, `${base}.ts`].find(p => fs.existsSync(p) && fs.statSync(p).isFile());
          assert.ok(candidate, `Unresolved ${id}`);
          return req(candidate);
        }
        return require(id);
      },
    }, { filename });
    if (filename.endsWith('/ui/tabs.tsx')) { const C = exports.TabsContent; exports.TabsContent = p => React.createElement(C, { ...p, forceMount: true }); }
    return exports;
  }
  return { Page: req(path.join(root, 'src/pages/segments.tsx')).default, requests };
}

// Toast removal timers are ~17 min; unref so they never hold the test process open.
const unrefTimeout = (...a) => { const t = setTimeout(...a); t.unref?.(); return t; };
const created = []; const revoked = [];
const TestURL = Object.assign(function (...a) { return new URL(...a); }, { createObjectURL: b => { created.push(b); return 'blob:test'; }, revokeObjectURL: u => revoked.push(u) });
dom.window.HTMLAnchorElement.prototype.click = function () {};
const clients = [];
test.afterEach(() => { cleanup(); for (const c of clients.splice(0)) { c.cancelQueries(); c.clear(); } });
function mount(role, url) {
  const { Page, requests } = load(role);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { gcTime: 0 } } });
  clients.push(client);
  const routes = ['/segments', '/segments/new', '/segments/:id', '/segments/:id/edit'].map(p => React.createElement(Route, { key: p, path: p, element: React.createElement(Page) }));
  const view = render(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: [url] }, React.createElement(Routes, null, ...routes))));
  return { view, requests };
}

test('list shows real columns, filters by status, analyst can create', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('analyst', '/segments');
  await waitFor(() => assert.ok(view.getByTestId('row-segment-7')));
  const row = view.getByTestId('row-segment-7').textContent;
  for (const s of ['Lapsed, LTV 500+', '12,340', '−120', 'Rae Analyst', 'active']) assert.ok(row.includes(s), s);
  assert.ok(view.getByTestId('button-new-segment'));
  fireEvent.change(view.getByTestId('select-filter-status'), { target: { value: 'draft' } });
  await waitFor(() => assert.ok(requests.some(r => r.url.includes('status=draft'))));
  assert.ok(!/donor/i.test(view.container.textContent.replace(/donor_crm/g, '')), 'partner copy only');
});

test('new audience starts blank and makes no count request until a rule exists; debounced preview after', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('analyst', '/segments/new');
  await waitFor(() => assert.ok(view.getByTestId('empty-tree')));
  assert.ok(view.getByTestId('text-summary').textContent.includes('add a rule'));
  assert.equal(requests.filter(r => r.url.endsWith('/preview')).length, 0);
  fireEvent.click(view.getByTestId('button-add-rule-root'));
  await waitFor(() => assert.ok(view.getByTestId('rule-0')));
  // enum "in" starts empty -> invalid -> still no preview
  await new Promise(r => setTimeout(r, 900));
  assert.equal(requests.filter(r => r.url.endsWith('/preview')).length, 0);
  assert.ok(view.getByText(/root\.rules\[0\]\.value: Choose at least one value/));
  fireEvent.click(view.getByTestId('button-delete-root.rules[0]'));
  fireEvent.click(view.getByTestId('button-add-group-root'));
  await waitFor(() => assert.ok(view.getByTestId('group-0')));
  fireEvent.click(view.getByTestId('toggle-negate-root.rules[0]'));
  assert.ok(view.getByTestId('text-summary').textContent.includes('not'));
});

test('save validates the name before calling the API', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('analyst', '/segments/new');
  await waitFor(() => assert.ok(view.getByTestId('button-save-draft')));
  fireEvent.click(view.getByTestId('button-save-draft'));
  assert.ok(view.getByText('Name this audience before saving.'));
  assert.equal(requests.filter(r => r.method === 'POST' && r.url === '/api/segments').length, 0);
});

test('edit loads definition, runs debounced preview and shows count; analyst JSON is read-only', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('analyst', '/segments/7/edit');
  await waitFor(() => assert.ok(view.getByTestId('rule-0')));
  await waitFor(() => assert.ok(view.getByTestId('text-live-count').textContent.includes('4,821')), { timeout: 3000 });
  const body = JSON.parse(requests.find(r => r.url.endsWith('/preview')).body);
  assert.deepEqual(body.definition, definition);
  assert.ok(view.getByTestId('textarea-json').readOnly);
  assert.equal(view.queryByTestId('button-apply-json'), null);
  assert.equal(view.queryByTestId('button-toggle-explain'), null);
});

test('admin JSON is editable with validation by node path, plus EXPLAIN panel', async (t) => {
  t.after(cleanup);
  const { view } = mount('admin', '/segments/7/edit');
  await waitFor(() => assert.ok(view.getByTestId('button-apply-json')));
  assert.equal(view.getByTestId('textarea-json').readOnly, false);
  fireEvent.change(view.getByTestId('textarea-json'), { target: { value: JSON.stringify({ version: 1, root: { op: 'and', rules: [{ type: 'trait', field: 'nope', operator: 'eq', value: 1 }] } }) } });
  fireEvent.click(view.getByTestId('button-validate-json'));
  assert.ok(view.getByTestId('list-json-issues').textContent.includes('root.rules[0].field'));
  assert.ok(view.getByTestId('button-toggle-explain'));
});

test('viewer: builder fully read-only, no mutation buttons', async (t) => {
  t.after(cleanup);
  const { view } = mount('viewer', '/segments/7/edit');
  await waitFor(() => assert.ok(view.getByTestId('banner-readonly')));
  for (const id of ['button-save-draft', 'button-save-active', 'button-add-rule-root', 'button-delete-root.rules[0]', 'button-apply-json']) assert.equal(view.queryByTestId(id), null, id);
  assert.ok(view.getByTestId('input-segment-name').disabled);
});

test('detail shows history, members, activations and audit from API', async (t) => {
  t.after(cleanup);
  const { view } = mount('analyst', '/segments/7');
  await waitFor(() => assert.ok(view.getByTestId('text-segment-name')));
  assert.equal(view.getByTestId('text-detail-count').textContent, '12.3K');
  await waitFor(() => assert.ok(view.getByTestId('row-activation-4')));
  assert.ok(view.getByTestId('row-audit-0').textContent.includes('segment.update'));
  await waitFor(() => assert.ok(view.getByTestId('row-member-101')));
  assert.ok(view.getByTestId('chart-count-history'));
  assert.ok(view.getByTestId('button-archive'));
});

test('viewer detail hides members and mutations', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('viewer', '/segments/7');
  await waitFor(() => assert.ok(view.getByTestId('members-restricted')));
  assert.equal(view.queryByTestId('button-archive'), null);
  assert.equal(view.queryByTestId('button-duplicate'), null);
  assert.equal(requests.some(r => r.url.includes('/members')), false);
  await act(async () => {});
});

test('save sends API refresh_schedule DTO: daily by default, null for manual', async (t) => {
  t.after(cleanup);
  const { view, requests } = mount('analyst', '/segments/7/edit');
  await waitFor(() => assert.ok(view.getByTestId('rule-0')));
  assert.equal(view.getByTestId('select-schedule').value, 'daily');
  fireEvent.click(view.getByTestId('button-save-draft'));
  await waitFor(() => assert.ok(requests.some(r => r.method === 'PUT')));
  assert.equal(JSON.parse(requests.find(r => r.method === 'PUT').body).refresh_schedule, 'daily');
  cleanup();
  const second = mount('analyst', '/segments/7/edit');
  await waitFor(() => assert.ok(second.view.getByTestId('rule-0')));
  fireEvent.change(second.view.getByTestId('select-schedule'), { target: { value: 'manual' } });
  fireEvent.click(second.view.getByTestId('button-save-active'));
  await waitFor(() => assert.ok(second.requests.some(r => r.method === 'PUT')));
  const body = JSON.parse(second.requests.find(r => r.method === 'PUT').body);
  assert.equal(body.refresh_schedule, null); assert.equal(body.status, 'active');
});


test('history: table toggle for all roles; analyst CSV downloads server response and revokes URL', async () => {
  const { view, requests } = mount('analyst', '/segments/7');
  await waitFor(() => assert.ok(view.getByTestId('button-export-csv-chart')));
  fireEvent.click(view.getByTestId('button-toggle-table-chart'));
  assert.ok(view.getByTestId('table-history-chart').textContent.includes('12,340'));
  fireEvent.click(view.getByTestId('button-export-csv-chart'));
  await waitFor(() => assert.ok(revoked.includes('blob:test')));
  assert.ok(requests.some(r => r.method === 'POST' && r.url === '/api/segments/7/count-history/export'));
  assert.equal(created.length, 1);
});

test('history: viewer gets table toggle but no CSV on detail or builder sparkline', async () => {
  const { view, requests } = mount('viewer', '/segments/7');
  await waitFor(() => assert.ok(view.getByTestId('button-toggle-table-chart')));
  assert.equal(view.queryByTestId('button-export-csv-chart'), null);
  cleanup();
  const b = mount('viewer', '/segments/7/edit');
  await waitFor(() => assert.ok(b.view.getByTestId('button-toggle-table-sparkline')));
  assert.equal(b.view.queryByTestId('button-export-csv-sparkline'), null);
  assert.equal([...requests, ...b.requests].some(r => r.url.includes('/export')), false);
});

test('history: analyst builder sparkline has CSV control', async () => {
  const { view } = mount('analyst', '/segments/7/edit');
  await waitFor(() => assert.ok(view.getByTestId('button-export-csv-sparkline')));
  assert.equal(view.getByTestId('button-export-csv-sparkline').disabled, false);
});
