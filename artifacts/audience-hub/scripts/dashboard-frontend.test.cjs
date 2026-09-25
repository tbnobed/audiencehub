const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const root = path.resolve(__dirname, '..');

function load(file, mocks = {}, globals = {}) {
  const source = fs.readFileSync(path.join(root, file), 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, {
    exports, require: id => id in mocks ? mocks[id] : require(id),
    AbortController, DOMException, Headers, setTimeout, clearTimeout, URLSearchParams, ...globals,
  });
  return exports;
}

test('five card queries are independent, range-keyed and never use legacy aggregates', async () => {
  const requests = [];
  const hooks = load('src/hooks/use-dashboards.ts', {
    '@tanstack/react-query': { useQuery: options => options },
    '@/lib/api': { fetchApi: async (url, options) => { requests.push({ url, options }); return {}; } },
    '@/lib/date-range': { validateCustomRange: () => null },
  });
  const controller = new AbortController();
  for (const card of ['kpis', 'giving-by-month', 'needs-attention', 'campaigns', 'partner-status']) {
    const query = hooks.useOverviewCard(card, '2026-01-01', '2026-12-31');
    assert.equal(query.retry, false);
    assert.equal(query.retryOnMount, false);
    assert.equal(query.queryKey.join('|'), `dashboard|overview|${card}|2026-01-01|2026-12-31`);
    await query.queryFn({ signal: controller.signal });
    assert.equal(requests.at(-1).url, `/api/dashboards/overview/${card}?from=2026-01-01&to=2026-12-31`);
    assert.equal(requests.at(-1).options.signal, controller.signal);
  }
  assert.equal(hooks.useDashboard('overview', '2026-01-01', '2026-12-31').enabled, false);
  assert.equal(requests.length, 5);
});

test('settings PATCH sends only fiscal month when dashboard default is unchanged', async () => {
  const requests = [];
  const resolved = {
    fiscal_year_start_month: 11, dashboard_default_preset: 'custom',
    dashboard_default_from: '2020-01-01', dashboard_default_to: '2024-12-31',
  };
  const hooks = load('src/hooks/use-dashboards.ts', {
    '@tanstack/react-query': { useQuery: options => options, useMutation: options => options, useQueryClient: () => ({}) },
    '@/lib/api': { fetchApi: async (url, options) => { requests.push({ url, options }); return resolved; } },
    '@/lib/date-range': load('src/lib/date-range.ts'),
  });
  const patch = hooks.useUpdateAdminSettings();
  assert.deepEqual(JSON.parse(JSON.stringify(await patch.mutationFn({ fiscal_year_start_month: 11 }))), resolved);
  assert.equal(requests[0].url, '/api/admin/settings');
  assert.equal(requests[0].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(requests[0].options.body), { fiscal_year_start_month: 11 });
  assert.deepEqual(JSON.parse(JSON.stringify(await hooks.useDashboardSettings().queryFn({ signal: new AbortController().signal }))), resolved);
});

test('admin default range form loads persisted dates, validates changes, and sends fiscal setting together', async () => {
  const { JSDOM } = require('jsdom');
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/admin' });
  const old = Object.fromEntries(['window', 'document', 'navigator', 'HTMLElement', 'MutationObserver'].map(key => [key, globalThis[key]]));
  for (const key of Object.keys(old)) globalThis[key] = dom.window[key];
  const { render, fireEvent, waitFor, cleanup } = require('@testing-library/react');
  const dateRange = load('src/lib/date-range.ts');
  let saved;
  const settings = {
    data: { fiscal_year_start_month: 10, dashboard_default_preset: 'custom', dashboard_default_from: '2020-01-01', dashboard_default_to: '2024-12-31' },
  };
  const update = { isPending: false, mutate: value => { saved = value; }, reset() {} };
  const Admin = load('src/pages/admin.tsx', {
    '@/hooks/use-auth': { useAuth: () => ({ user: { role: 'admin' } }) },
    '@/hooks/use-dashboards': { useAdminSettings: () => settings, useUpdateAdminSettings: () => update },
    '@/lib/date-range': dateRange,
    '@/components/ui/button': { Button: props => React.createElement('button', props) },
    'lucide-react': { RefreshCw: () => null, Settings2: () => null },
  }).default;
  try {
    const view = render(React.createElement(Admin));
    await waitFor(() => assert.equal(view.getByTestId('default-range-from').value, '2020-01-01'));
    assert.equal(view.getByTestId('default-range-to').value, '2024-12-31');
    fireEvent.change(view.getByTestId('default-range-to'), { target: { value: '2019-01-01' } });
    assert.equal(view.getByTestId('save-settings').disabled, true);
    assert.match(view.getByRole('alert').textContent, /on or before/);
    fireEvent.click(view.getByRole('button', { name: /Use seeded dates/ }));
    fireEvent.change(view.getByTestId('default-range-from'), { target: { value: '2021-01-01' } });
    fireEvent.click(view.getByTestId('save-settings'));
    assert.deepEqual(JSON.parse(JSON.stringify(saved)), {
      fiscal_year_start_month: 10, dashboard_default_preset: 'custom',
      dashboard_default_from: '2021-01-01', dashboard_default_to: '2024-12-31',
    });
    fireEvent.click(view.getByLabelText('Last 90 days'));
    fireEvent.click(view.getByTestId('save-settings'));
    assert.equal(saved.dashboard_default_preset, '90d');
    assert.equal(saved.dashboard_default_from, null);
    assert.equal(saved.dashboard_default_to, null);
    fireEvent.click(view.getByLabelText('Custom date range'));
    fireEvent.click(view.getByRole('button', { name: /Use seeded dates/ }));
    fireEvent.change(view.getByTestId('fiscal-year-start-month'), { target: { value: '11' } });
    fireEvent.click(view.getByTestId('save-settings'));
    assert.deepEqual(JSON.parse(JSON.stringify(saved)), { fiscal_year_start_month: 11 },
      'a fiscal-only change must not pin the resolved environment default');
  } finally {
    cleanup();
    dom.window.close();
    for (const [key, value] of Object.entries(old)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});

test('cards render successes while other cards are pending or failed; each failure has Retry', () => {
  const results = {
    kpis: { error: new Error('KPIs unavailable') },
    'giving-by-month': {},
    'needs-attention': { data: { attention: [{ severity: 'warning', title: 'Review rejected rows', explanation: 'Import needs review', href: null }] } },
    campaigns: { error: new Error('Campaigns unavailable') },
    'partner-status': { data: { partner_status: { givers: 17, statuses: [], prospects: 23 } } },
  };
  const component = load('src/components/dashboard/overview.tsx', {
    '@/hooks/use-dashboards': { useOverviewCard: card => ({ ...results[card], isError: !!results[card].error, refetch() {}, isFetching: false }) },
    '@/components/ui/skeleton': { Skeleton: props => React.createElement('div', props) },
    '@/lib/utils': { cn: (...parts) => parts.filter(Boolean).join(' ') },
    '@/lib/date-range': { millionsTick: () => String },
  });
  const html = renderToStaticMarkup(React.createElement(component.OverviewView, {
    from: '2026-01-01', to: '2026-12-31', canImport: false, onWiden() {}, periodNoun: 'year',
  }));
  assert.match(html, /17 givers/);
  assert.match(html, /Review rejected rows/);
  assert.match(html, /Loading giving by month/);
  assert.match(html, /KPIs unavailable/);
  assert.match(html, /Campaigns unavailable/);
  assert.equal((html.match(/>Retry<\/button>/g) || []).length, 2);
  assert.doesNotMatch(html, /Nothing needs attention/);
});

test('dashboard timeout is 15 seconds, aborts stalled bodies and leaves upload writes unbounded', async () => {
  const timers = [];
  let requestSignal;
  const api = load('src/lib/api.ts', {}, {
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
    fetch: async (_url, options) => { requestSignal = options.signal; return { ok: true, json: () => new Promise(() => {}) }; },
  });
  assert.equal(api.API_TIMEOUT_MS, 15000);
  const request = api.fetchApi('/api/dashboards/overview/kpis');
  assert.equal(timers[0].ms, 15000);
  timers[0].fn();
  await assert.rejects(request, /timed out after 15 seconds/);
  assert.equal(requestSignal.aborted, true);
  const controller = new AbortController();
  const upload = api.fetchApi('/api/imports/upload', { method: 'POST', signal: controller.signal });
  assert.equal(timers.length, 1);
  controller.abort();
  await assert.rejects(upload, /cancelled/);
});

test('built CSS contains only emitted local WOFF2 font files', () => {
  const assets = path.join(root, 'dist/public/assets');
  const css = fs.readdirSync(assets).filter(name => name.endsWith('.css'))
    .map(name => fs.readFileSync(path.join(assets, name), 'utf8')).join('\n');
  const faces = css.match(/@font-face\s*\{[^}]+\}/g) || [];
  assert.equal(faces.length, 7);
  for (const face of faces) {
    assert.doesNotMatch(face, /data:|https?:|\.woff[)"']/);
    const url = face.match(/url\(["']?([^)"']+\.woff2)["']?\)/)?.[1];
    assert.ok(url, 'font must reference a bundled woff2');
    assert.ok(fs.existsSync(path.join(root, 'dist/public', url)), `missing font ${url}`);
  }
  assert.doesNotMatch(css, /fonts\.googleapis|fonts\.gstatic|data:[^)]*(?:woff|font)/);
});