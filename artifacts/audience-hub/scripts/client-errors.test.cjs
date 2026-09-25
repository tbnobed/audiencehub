const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function load(file, mocks, dev = false) {
  const source = fs.readFileSync(path.join(__dirname, '..', file), 'utf8')
    .replaceAll('import.meta.env.DEV', String(dev));
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, Error, window: { location: { pathname: '/system' } },
    require: id => id in mocks ? mocks[id] : require(id) });
  return exports;
}

test('reporter transmits only safe categories, routes, component names and explicit profile IDs', async () => {
  const calls = [];
  const { reportClientError } = load('src/lib/client-errors.ts', {
    './api': { fetchApi: async (url, options) => calls.push({ url, ...options }) },
  });
  const secret = 'Jane Smith private@example.org +15551234567 cookie=secret';
  await reportClientError(new TypeError(secret), {
    componentStack: `\n at ProfileDetail (https://private.test/?${secret})\n at JaneSmith\n at Card (/private/path)`,
  }, `/profiles/42?${secret}`, '42');
  const body = JSON.parse(calls[0].body);
  assert.deepEqual(body, { category: 'TypeError', route: '/profiles/42', profile_id: 42,
    component_stack: ['ProfileDetail', 'Card'] });
  assert.equal(calls[0].url, '/api/client-errors');
  assert.equal(calls[0].timeoutMs, 5000);
  assert.doesNotMatch(JSON.stringify(calls), /Jane|private|15551234567|cookie/);
  await reportClientError(secret, { componentStack: secret }, 'https://example.org/person/Jane', 'Jane');
  assert.equal(JSON.parse(calls[1].body).route, '/unknown');
  assert.equal(JSON.parse(calls[1].body).profile_id, null);
});

test('reporter failures never throw or recursively report', async () => {
  let count = 0;
  const { reportClientError } = load('src/lib/client-errors.ts', {
    './api': { fetchApi: async () => { count++; throw new Error('network'); } },
  });
  await assert.doesNotReject(reportClientError(new Error('private'), {}, '/'));
  assert.equal(count, 1);
});

for (const dev of [true, false]) {
  test(`global boundary ${dev ? 'development shows full details' : 'production hides details'} and reports once`, () => {
    const calls = [];
    const { ErrorBoundary } = load('src/components/error-boundary.tsx', {
      '@/lib/client-errors': { reportClientError: (...args) => { calls.push(args); return Promise.resolve(); } },
    }, dev);
    const boundary = new ErrorBoundary({ children: 'normal' });
    boundary.setState = value => { boundary.state = { ...boundary.state, ...value }; };
    const error = new Error('private profile name');
    boundary.state = ErrorBoundary.getDerivedStateFromError(error);
    boundary.componentDidCatch(error, { componentStack: '\n at PrivateComponent (private/path)' });
    const html = renderToStaticMarkup(boundary.render());
    assert.equal(calls.length, 1);
    if (dev) {
      assert.match(html, /private profile name/);
      assert.match(html, /PrivateComponent/);
    } else {
      assert.doesNotMatch(html, /private|PrivateComponent/);
    }
    boundary.resetError();
    assert.equal(boundary.render(), 'normal');
  });
}