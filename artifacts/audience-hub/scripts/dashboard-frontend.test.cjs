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