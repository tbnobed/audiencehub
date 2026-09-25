const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/profiles/1' });
for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node',
  'MutationObserver', 'CustomEvent', 'Event', 'MouseEvent', 'getComputedStyle']) {
  Object.defineProperty(globalThis, name, { value: dom.window[name], configurable: true });
}
globalThis.requestAnimationFrame = callback => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = clearTimeout;
const React = require('react');
const { render, cleanup, waitFor, fireEvent } = require('@testing-library/react');
const { MemoryRouter, Routes, Route } = require('react-router-dom');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
const root = path.resolve(__dirname, '..');
const fixturePath = process.env.PROFILE_CAPTURE_FILE || path.join(__dirname, 'fixtures/profiles-medium.json');
// Absence is deliberately fatal: this regression must not silently skip in CI.
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

function loadPage(response, dev = false) {
  const cache = new Map();
  const requests = [];
  const transport = async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/traits') return new Response(JSON.stringify(fixture.catalog));
    if (url === '/api/client-errors') return new Response('{}');
    assert.equal(url, `/api/profiles/${response.id}`, 'unexpected API request');
    return new Response(JSON.stringify(response.body), { status: response.status });
  };
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename);
    const source = filename.endsWith('profile-detail.tsx') && process.env.PROFILE_PAGE_SOURCE
      ? fs.readFileSync(process.env.PROFILE_PAGE_SOURCE, 'utf8')
      : fs.readFileSync(filename, 'utf8');
    const output = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
        target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;
    const exports = {};
    cache.set(filename, exports);
    vm.runInNewContext(output.replaceAll('import.meta.env.DEV', String(dev)), {
      exports, console, URL, URLSearchParams, setTimeout, clearTimeout,
      fetch: transport, Headers, Response, AbortController, DOMException,
      window, document, navigator, HTMLElement, getComputedStyle,
      requestAnimationFrame, cancelAnimationFrame,
      require: id => {
        if (id.startsWith('@/') || id.startsWith('.')) {
          const base = id.startsWith('@/') ? path.join(root, 'src', id.slice(2))
            : path.resolve(path.dirname(filename), id);
          const candidate = [base, `${base}.tsx`, `${base}.ts`].find(p => fs.existsSync(p) && fs.statSync(p).isFile());
          assert.ok(candidate, `Unresolved local dependency ${id}`);
          return load(candidate);
        }
        return require(id);
      },
    }, { filename });
    if (filename.endsWith('/ui/tabs.tsx')) {
      const ActualContent = exports.TabsContent;
      exports.TabsContent = props => React.createElement(ActualContent, { ...props, forceMount: true });
    }
    return exports;
  }
  return {
    Page: load(path.join(root, 'src/pages/profile-detail.tsx')).default,
    Boundary: load(path.join(root, 'src/components/ProfileSectionBoundary.tsx')).ProfileSectionBoundary,
    requests,
  };
}

test('capture provenance and mandatory coverage are intact', () => {
  assert.equal(fixture.generator, 'app.seed.generate_seed');
  assert.equal(fixture.scale, 'medium');
  assert.equal(fixture.random_seed, 20250308);
  if (!process.env.PROFILE_CAPTURE_FILE) {
    assert.equal(fixture.full_medium, true);
    assert.equal(fixture.requested_profiles, 50000);
    assert.match(fixture.api_source_sha256, /^[0-9a-f]{64}$/);
    assert.equal(Object.keys(fixture.seed_csv_sha256).length, 5);
  }
  assert.deepEqual(fixture.pipeline,
    ['run_import', 'resolve_batch', 'recompute_traits', 'FastAPI TestClient']);
  assert.equal(fixture.imports.length, 5);
  for (const row of fixture.imports) {
    assert.equal(row.status, 'completed');
    assert.equal(row.rows_ok + row.rows_rejected, row.rows_total);
  }
  for (const role of ['admin', 'viewer']) {
    for (let id = 1; id <= 50; id++) {
      assert.ok(fixture.responses.some(row => row.role === role &&
        row.id === id && row.label === `profile-${id}` && row.status === 200));
    }
    for (const label of ['no-traits-row', 'prospect', 'lapsed', 'no-email', 'no-phone',
      'no-consents', 'no-enrichment', 'expired-enrichment', 'merged',
      'events-no-gifts', 'five9-only']) {
      assert.ok(fixture.responses.some(row => row.role === role && row.label === label), label);
    }
  }
});

for (const response of fixture.responses) {
  test(`${response.role}: ${response.label} mounts real query, router and Radix tabs`, async () => {
    const { Page, requests } = loadPage(response);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    try {
      const view = render(React.createElement(QueryClientProvider, { client },
        React.createElement(MemoryRouter, { initialEntries: [`/profiles/${response.id}`] },
          React.createElement(Routes, null,
            React.createElement(Route, { path: '/profiles/:id', element: React.createElement(Page) })))));
      await waitFor(() => assert.ok(view.container.textContent.includes(
        response.status === 200 ? response.body.first_name || 'Unnamed Profile' : 'Profile Not Found')));
      if (response.status !== 200) {
        assert.ok(view.getByText('Retry'));
        return;
      }
      for (const tab of view.getAllByRole('tab')) {
        fireEvent.mouseDown(tab, { button: 0, ctrlKey: false });
      }
      const html = view.container.innerHTML;
      if (response.label === 'events-no-gifts' || response.label === 'five9-only') {
        assert.ok(response.body.events.length > 0);
        assert.equal(response.body.gifts.length, 0);
      }
      if (response.label === 'five9-only') {
        assert.ok(response.body.source_records.length > 0);
        assert.ok(response.body.source_records.every(row => row.source_key === 'five9'));
      }
      if (response.label === 'no-email') assert.equal(response.body.email, null);
      if (response.label === 'no-phone') assert.equal(response.body.phone, null);
      if (response.label === 'no-consents') assert.equal(response.body.consents.length, 0);
      if (['no-enrichment', 'expired-enrichment'].includes(response.label)) {
        assert.equal(response.body.enrichment.length, 0);
      }
      if (response.label === 'no-traits-row') {
        assert.equal(response.body.traits.computed_at, null);
      }
      assert.match(html, /button-back-profiles/);
      assert.match(html, /Consent Records/);
      assert.match(html, /Source Records/);
      assert.match(html, /Enrichment Data/);
      assert.doesNotMatch(html, /Something went wrong|Couldn't load this section|Unable to render|Invalid Date|NaN/);
      assert.equal(requests.filter(r => r.url === `/api/profiles/${response.id}`).length, 1);
      assert.equal(requests.filter(r => r.url === '/api/client-errors').length, 0);
    } finally {
      cleanup();
      client.clear();
    }
  });
}

for (const dev of [false, true]) {
  test(`section boundary actually catches render throw, retries, reports; dev=${dev}`, async () => {
    const { Boundary, requests } = loadPage(fixture.responses[0], dev);
    let broken = true;
    const originalError = console.error;
    console.error = () => {}; // React deliberately reports the injected render error.
    try {
      const view = render(React.createElement('div', null,
        React.createElement('p', null, 'Other section remains visible'),
        React.createElement(Boundary, { section: 'Gifts', route: '/profiles/1',
          profileId: 1, onRetry: () => { broken = false; } }, () => {
          if (broken) throw new Error('intentional section test');
          return React.createElement('p', null, 'Recovered gifts');
        })));
      assert.ok(view.getByRole('alert', { name: 'Gifts' }));
      assert.ok(view.getByText('Other section remains visible'));
      assert.equal(view.container.textContent.includes('intentional section test'), dev);
      await waitFor(() => assert.equal(requests.filter(r => r.url === '/api/client-errors').length, 1));
      const report = JSON.parse(requests.find(r => r.url === '/api/client-errors').options.body);
      assert.equal(report.profile_id, 1);
      assert.equal(report.route, '/profiles/1');
      assert.ok(!JSON.stringify(report).includes('intentional section test'));
      fireEvent.click(view.getByRole('button', { name: 'Retry' }));
      await waitFor(() => assert.ok(view.getByText('Recovered gifts')));
    } finally {
      cleanup();
      console.error = originalError;
    }
  });
}