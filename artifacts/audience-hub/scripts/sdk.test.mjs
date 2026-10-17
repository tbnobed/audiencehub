import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const bundle = await readFile(new URL('../dist/sdk/ah.js', import.meta.url), 'utf8');

function browser(options = {}) {
  const cookies = new Map(Object.entries(options.cookies || {}));
  const storage = new Map(Object.entries(options.storage || {}));
  const requests = [], beacons = [], warnings = [], intervals = new Map(), timeouts = new Map(), listeners = {};
  let sequence = 0, timerId = 0;
  const document = { title: 'Acceptance', referrer: 'https://example.test/' };
  Object.defineProperty(document, 'cookie', {
    get() { if (options.noCookies) return ''; return [...cookies].map(([k, v]) => `${k}=${v}`).join('; '); },
    set(value) {
      if (options.noCookies) return;
      const [key, content] = value.split(';')[0].split('=');
      cookies.set(key, content);
      document.lastCookie = value;
    },
  });
  const localStorage = {
    getItem(key) { if (options.noStorage) throw Error('Storage disabled'); return storage.get(key) || null; },
    setItem(key, value) { if (options.noStorage) throw Error('Storage disabled'); storage.set(key, value); },
  };
  const window = {
    ahConfig: { writeKey: 'browser-key', host: 'https://hub.test', ...options.config },
    ah: { q: options.queue || [] },
    addEventListener(name, fn) { listeners[name] = fn; },
  };
  const context = vm.createContext({
    window, document, localStorage,
    location: { origin: 'https://site.test', href: 'https://site.test/watch', pathname: '/watch', protocol: 'https:' },
    navigator: {
      globalPrivacyControl: options.gpc,
      sendBeacon(url, body) { beacons.push({ url, body }); return options.beaconOK !== false; },
    },
    crypto: { randomUUID: () => `uuid-${++sequence}` },
    console: { warn: message => warnings.push(message) },
    URL, Blob, TextEncoder, AbortController,
    setInterval(fn, delay) { const id = ++timerId; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn, delay) { const id = ++timerId; timeouts.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    fetch(url, init) {
      requests.push({ url, ...init, json: JSON.parse(init.body) });
      return options.fetch ? options.fetch(url, init) : Promise.resolve({ ok: true, status: 202 });
    },
  });
  vm.runInContext(bundle, context);
  const tick = async () => {
    for (const { fn } of [...intervals.values()]) fn();
    await Promise.resolve();
    await Promise.resolve();
  };
  return { sdk: window.ah, context, cookies, storage, requests, beacons, warnings, intervals, timeouts, listeners, document, tick };
}

test('minified standalone bundle is strictly less than 5KB', () => {
  assert.ok(Buffer.byteLength(bundle) < 5000);
  assert.ok(!/\bimport\s/.test(bundle));
});

test('build generates actual SHA-256 metadata and embeds the matching hash in acceptance HTML', async () => {
  const manifest = JSON.parse(await readFile(new URL('../dist/sdk/manifest.json', import.meta.url), 'utf8'));
  assert.equal(manifest.file, 'ah.js');
  assert.equal(manifest.sha256, createHash('sha256').update(bundle).digest('hex'));
  assert.equal(manifest.url, `/sdk/ah.js?v=${manifest.sha256}`);
  const html = await readFile(new URL('../dist/sdk/test.html', import.meta.url), 'utf8');
  assert.ok(html.includes(`ah.js?v=${manifest.sha256}`));
  assert.ok(!html.includes('__AH_SDK_SHA256__'));
});

test('preload queue is replayed in order; page and identify share persisted anonymous ID', async () => {
  const b = browser({ queue: [['page', 'Before load'], ['track', 'Watched', { percent: 75 }], ['identify', 'crm-123', { email: 'donor@example.test' }]] });
  assert.equal(b.requests.length, 0);
  await b.tick();
  const batch = b.requests[0].json.batch;
  assert.deepEqual(batch.map(e => e.type), ['page', 'track', 'identify']);
  assert.equal(new Set(batch.map(e => e.anonymousId)).size, 1);
  assert.equal(batch[0].userId, undefined);
  assert.equal(batch[2].userId, 'crm-123');
  assert.equal(batch[0].properties.path, '/watch');
  assert.equal(b.cookies.get('ah_aid'), batch[0].anonymousId);
  assert.match(b.document.lastCookie, /Max-Age=31536000; SameSite=Lax; Secure/);
  assert.equal(b.storage.get('ah_aid'), batch[0].anonymousId);
  assert.equal(new Set(batch.map(e => e.messageId)).size, 3);
});

test('first party cookie wins, localStorage fallback survives unavailable cookies', async () => {
  const cookie = browser({ cookies: { ah_aid: 'from-cookie' }, storage: { ah_aid: 'old-local' } });
  cookie.sdk.track('Test');
  await cookie.sdk.flush();
  assert.equal(cookie.requests[0].json.batch[0].anonymousId, 'from-cookie');
  const fallback = browser({ noCookies: true, storage: { ah_aid: 'from-local' } });
  fallback.sdk.track('Test');
  await fallback.sdk.flush();
  assert.equal(fallback.requests[0].json.batch[0].anonymousId, 'from-local');
  const disabled = browser({ noCookies: true, noStorage: true });
  disabled.sdk.track('Test');
  await disabled.sdk.flush();
  assert.equal(disabled.requests.length, 1);
});

test('flushes at exactly 20 events or a 2 second interval using keepalive and header auth', async () => {
  const b = browser();
  assert.equal([...b.intervals.values()][0].delay, 2000);
  for (let i = 0; i < 19; i++) b.sdk.track('Test', { i });
  assert.equal(b.requests.length, 0);
  b.sdk.track('Test', { i: 19 });
  assert.equal(b.requests.length, 1);
  assert.equal(b.requests[0].json.batch.length, 20);
  assert.equal(b.requests[0].keepalive, true);
  assert.equal(b.requests[0].credentials, 'omit');
  assert.equal(b.requests[0].headers['X-Write-Key'], 'browser-key');
  assert.equal(b.requests[0].url, 'https://hub.test/v1/batch');
  await new Promise(resolve => setImmediate(resolve));
  b.sdk.track('Next');
  await b.tick();
  assert.equal(b.requests.length, 2);
});

test('failed delivery retries identical message IDs and payloads, then stops after 3 attempts', async () => {
  const b = browser({ fetch: () => Promise.resolve({ ok: false, status: 503 }) });
  b.sdk.track('Retry');
  for (let i = 0; i < 6; i++) await b.tick();
  assert.equal(b.requests.length, 3);
  assert.deepEqual(b.requests.map(r => r.body), Array(3).fill(b.requests[0].body));
  assert.ok(b.warnings.some(w => w.includes('retry limit')));
});

test('network errors and 429 retry; permanent validation/auth errors do not', async () => {
  for (const status of [400, 401, 403, 413, 422]) {
    const b = browser({ fetch: () => Promise.resolve({ ok: false, status }) });
    b.sdk.track('Rejected');
    await b.tick();
    await b.tick();
    assert.equal(b.requests.length, 1);
    assert.ok(b.warnings.some(w => w.includes('rejected')));
  }
  for (const fetch of [() => Promise.reject(Error('network')), () => Promise.resolve({ ok: false, status: 429 })]) {
    const b = browser({ fetch });
    b.sdk.track('Retry');
    await b.tick();
    await b.tick();
    assert.equal(b.requests.length, 2);
  }
});

test('pagehide uses beacon body write_key; never query credentials or duplicate in-flight fetch', async () => {
  let resolve;
  const b = browser({ fetch: () => new Promise(r => { resolve = r; }) });
  b.sdk.track('Fetch-owned');
  const flushing = b.sdk.flush();
  b.sdk.track('Beacon-owned');
  b.listeners.pagehide();
  assert.equal(b.beacons.length, 1);
  assert.equal(b.beacons[0].url, 'https://hub.test/v1/batch');
  const body = JSON.parse(await b.beacons[0].body.text());
  assert.equal(body.write_key, 'browser-key');
  assert.equal(body.batch[0].event, 'Beacon-owned');
  assert.notEqual(body.batch[0].messageId, b.requests[0].json.batch[0].messageId);
  b.listeners.pagehide();
  assert.equal(b.beacons.length, 1);
  resolve({ ok: true, status: 202 });
  await flushing;
  await b.tick();
  assert.equal(b.requests.length, 1);
});

test('unsuccessful beacon requeues unchanged IDs without unbounded retries', async () => {
  const b = browser({ beaconOK: false });
  b.sdk.track('Beacon retry');
  for (let i = 0; i < 5; i++) b.listeners.pagehide();
  assert.equal(b.beacons.length, 3);
  const bodies = await Promise.all(b.beacons.map(async r => JSON.parse(await r.body.text())));
  assert.equal(new Set(bodies.map(r => r.batch[0].messageId)).size, 1);
});

test('optOut clears queue, persists a cookie, aborts active request, and stops all future sends', async () => {
  const b = browser({ fetch: () => new Promise(() => {}) });
  b.sdk.track('Before');
  void b.sdk.flush();
  b.sdk.track('Queued');
  b.sdk.optOut();
  assert.equal(b.requests[0].signal.aborted, true);
  assert.equal(b.cookies.get('ah_optout'), '1');
  assert.equal(b.storage.get('ah_optout'), '1');
  assert.equal(b.intervals.size, 0);
  b.sdk.track('After');
  b.sdk.identify('never');
  await b.sdk.flush();
  b.listeners.pagehide();
  assert.equal(b.requests.length, 1);
  assert.equal(b.beacons.length, 0);
  const restart = browser({ cookies: { ah_optout: '1' }, queue: [['page']] });
  await restart.sdk.flush();
  assert.equal(restart.requests.length, 0);
  assert.equal(restart.cookies.has('ah_aid'), false);
});

test('queued optOut takes precedence before replay can trigger threshold flush', async () => {
  const b = browser({ queue: [...Array.from({ length: 25 }, () => ['track', 'Never send']), ['optOut']] });
  await b.tick();
  assert.equal(b.requests.length, 0);
  assert.equal(b.cookies.get('ah_optout'), '1');
});

test('GPC defaults to no identity, storage writes or sends; explicit ignore permits sends but not opt-out override', async () => {
  const b = browser({ gpc: true, queue: [['page']] });
  b.sdk.track('Ignored');
  await b.sdk.flush();
  assert.equal(b.cookies.size, 0);
  assert.equal(b.storage.size, 0);
  assert.equal(b.requests.length, 0);
  const ignored = browser({ gpc: true, config: { ignoreGPC: true } });
  ignored.sdk.page();
  await ignored.sdk.flush();
  assert.equal(ignored.requests.length, 1);
  const optOut = browser({ gpc: true, config: { ignoreGPC: true }, cookies: { ah_optout: '1' } });
  optOut.sdk.page();
  await optOut.sdk.flush();
  assert.equal(optOut.requests.length, 0);
});

test('bounded validation rejects malformed, oversized, deep and circular events', async () => {
  const b = browser();
  b.sdk.track('');
  b.sdk.track(42);
  b.sdk.identify('');
  b.sdk.track('Array', []);
  b.sdk.page('Array', []);
  b.sdk.track('Large', { value: 'x'.repeat(32000) });
  let deep = {};
  for (let i = 0; i < 8; i++) deep = { child: deep };
  b.sdk.track('Deep', deep);
  const cyclic = {}; cyclic.self = cyclic;
  b.sdk.track('Cycle', cyclic);
  await b.sdk.flush();
  assert.equal(b.requests.length, 0);
  b.sdk.track('Valid');
  await b.sdk.flush();
  assert.equal(b.requests[0].json.batch.length, 1);
});

test('pagehide drains multiple pending batches without exceeding the total beacon budget', async () => {
  let resolve;
  const b = browser({ fetch: () => new Promise(r => { resolve = r; }) });
  b.sdk.track('Active');
  const active = b.sdk.flush();
  for (let i = 0; i < 75; i++) b.sdk.track('Pending', { i });
  b.listeners.pagehide();
  const payloads = await Promise.all(b.beacons.map(async beacon => JSON.parse(await beacon.body.text())));
  assert.equal(payloads.reduce((sum, payload) => sum + payload.batch.length, 0), 75);
  assert.ok(b.beacons.reduce((sum, beacon) => sum + beacon.body.size, 0) < 60000);
  assert.ok(payloads.every(payload => payload.batch.length <= 20));
  assert.equal(new Set(payloads.flatMap(payload => payload.batch.map(e => e.messageId))).size, 75);
  resolve({ ok: true, status: 202 });
  await active;
});

test('bounded queue and batches stay under keepalive budget while fetch is busy', async () => {
  let resolve;
  const b = browser({ fetch: () => new Promise(r => { resolve = r; }) });
  b.sdk.track('Active');
  const active = b.sdk.flush();
  for (let i = 0; i < 120; i++) b.sdk.track('Queued', { i, value: 'x'.repeat(29000) });
  assert.equal(b.requests.length, 1);
  assert.ok(b.warnings.length >= 20);
  resolve({ ok: true, status: 202 });
  await active;
  b.listeners.pagehide();
  const raw = await b.beacons[0].body.text();
  assert.ok(Buffer.byteLength(raw) < 64000);
  assert.ok(JSON.parse(raw).batch.length <= 20);
});

test('invalid config cannot send or write an anonymous identity', async () => {
  for (const config of [{ writeKey: '' }, { host: 'javascript:alert(1)' }, { host: 'https://user:pass@host.test' }]) {
    const b = browser({ config });
    b.sdk.page();
    await b.sdk.flush();
    assert.equal(b.requests.length, 0);
    assert.equal(b.cookies.size, 0);
  }
});

test('request timeout aborts the in-flight request and preserves original message ID for retry', async () => {
  const b = browser({ fetch: (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
  }) });
  b.sdk.track('Timeout');
  const first = b.sdk.flush();
  const timer = [...b.timeouts.values()][0];
  assert.equal(timer.delay, 10000);
  timer.fn();
  await first;
  assert.equal(b.requests[0].signal.aborted, true);
  void b.sdk.flush();
  assert.equal(b.requests.length, 2);
  assert.equal(b.requests[0].json.batch[0].messageId, b.requests[1].json.batch[0].messageId);
});

test('duplicate script execution does not create another timer or replay sends', async () => {
  const b = browser({ queue: [['track', 'Once']] });
  const original = b.sdk;
  vm.runInContext(bundle, b.context);
  assert.equal(b.context.window.ah, original);
  assert.equal(b.intervals.size, 1);
  await b.tick();
  assert.equal(b.requests.length, 1);
  assert.equal(b.requests[0].json.batch.length, 1);
});

test('developer acceptance HTML boots the queue before appending async script and keeps key out of URLs/storage', async () => {
  const html = await readFile(new URL('../dist/sdk/test.html', import.meta.url), 'utf8');
  const manifest = JSON.parse(await readFile(new URL('../dist/sdk/manifest.json', import.meta.url), 'utf8'));
  const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, { value: '', disabled: false, textContent: '' });
    return elements.get(id);
  };
  get('key').value = 'acceptance-browser-key';
  let appended;
  const window = {};
  const context = vm.createContext({
    window, URL, location: { origin: 'https://current-browser.test', href: 'https://current-browser.test/sdk/test.html' },
    document: {
      getElementById: get,
      createElement: () => ({}),
      head: { appendChild(script) {
        assert.deepEqual(JSON.parse(JSON.stringify(window.ah.q)), [['page', 'SDK acceptance']]);
        assert.equal(window.ahConfig.writeKey, 'acceptance-browser-key');
        appended = script;
      } },
    },
  });
  // Browser globals alias window members; model that in this minimal VM.
  Object.defineProperty(context, 'ah', { get: () => window.ah });
  vm.runInContext(source, context);
  get('load').onclick();
  assert.equal(appended.async, true);
  assert.equal(appended.src, `https://current-browser.test/sdk/ah.js?v=${manifest.sha256}`);
  assert.equal(get('key').value, '');
  for (let i = 0; i < 200; i++) window.ah.track('Bounded');
  assert.equal(window.ah.q.length, 100);
  window.ah.optOut();
  assert.deepEqual(JSON.parse(JSON.stringify(window.ah.q)), [['optOut']]);
  assert.equal(window.ahConfig.host, 'https://current-browser.test');
});

test('container and fresh development startup include all SDK build inputs and served outputs', async () => {
  const docker = await readFile(new URL('../Dockerfile', import.meta.url), 'utf8');
  const dev = await readFile(new URL('../scripts/dev.sh', import.meta.url), 'utf8');
  assert.ok(docker.includes('vite.sdk.config.ts'));
  assert.ok(docker.includes('COPY scripts/sdk-build.mjs ./scripts/sdk-build.mjs'));
  assert.ok(docker.includes('COPY sdk/ ./sdk/'));
  assert.ok(docker.includes('COPY --from=web /web/dist/sdk ./static/sdk'));
  assert.ok(docker.indexOf('COPY sdk/') < docker.indexOf('npm run build'));
  assert.ok(dev.indexOf('npm run build:sdk') < dev.indexOf('python3 -m uvicorn'));
});
