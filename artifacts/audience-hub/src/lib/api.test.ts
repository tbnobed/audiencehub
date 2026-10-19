// Run: node --test src/lib/api.test.ts (Node >= 22.18)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiUrl, ApiError, ApiTimeoutError, fetchApi, setCsrfToken } from './api.ts';

test('API and download paths respect the artifact base without double-prefixing', () => {
  assert.equal(apiUrl('/api/activation-runs/12/download', '/audience-hub/'),
    '/audience-hub/api/activation-runs/12/download');
  assert.equal(apiUrl('/api/activations', '/'), '/api/activations');
  assert.equal(apiUrl('/audience-hub/api/activations', '/audience-hub/'),
    '/audience-hub/api/activations');
});

test('a never-resolving fetch times out and aborts the underlying request', async () => {
  const originalFetch = globalThis.fetch;
  let requestSignal: AbortSignal | undefined;
  globalThis.fetch = async (_input, init) => {
    requestSignal = init?.signal ?? undefined;
    return new Promise<Response>(() => {});
  };
  try {
    await assert.rejects(fetchApi('/api/dashboards/overview', { timeoutMs: 20 }), ApiTimeoutError);
    assert.equal(requestSignal?.aborted, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('caller cancellation aborts the request without reporting a timeout', async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  let requestSignal: AbortSignal | undefined;
  globalThis.fetch = async (_input, init) => {
    requestSignal = init?.signal ?? undefined;
    return new Promise<Response>(() => {});
  };
  try {
    const pending = fetchApi('/api/dashboards/giving', { signal: controller.signal, timeoutMs: 1000 });
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(requestSignal?.aborted, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a stalled response body also times out, but server errors remain visible', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(new ReadableStream({
      start() { /* The server never finishes the body. */ },
    }), { status: 200 });
    await assert.rejects(fetchApi('/api/dashboards/settings', { timeoutMs: 20 }), ApiTimeoutError);

    globalThis.fetch = async () => Response.json({ error: { message: 'Database unavailable' } }, { status: 503 });
    await assert.rejects(fetchApi('/api/dashboards/settings'), /Database unavailable/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('an anonymous session check returns a typed 401 without expiring its own query', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const events: string[] = [];
  Object.defineProperty(globalThis, 'window', { configurable: true,
    value: { dispatchEvent(event: Event) { events.push(event.type); return true; } } });
  globalThis.fetch = async () => Response.json({ error: { message: 'Not authenticated' } }, { status: 401 });
  try {
    await assert.rejects(fetchApi('/api/me'), error => error instanceof ApiError && error.status === 401);
    assert.deepEqual(events, []);
    await assert.rejects(fetchApi('/api/profiles'), error => error instanceof ApiError && error.status === 401);
    assert.deepEqual(events, ['kinship-session-expired']);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});

test('an explicit fresh CSRF token is not replaced by a stale cached token', async () => {
  const originalFetch = globalThis.fetch;
  let received = '';
  globalThis.fetch = async (_input, init) => {
    received = new Headers(init?.headers).get('X-CSRF-Token') ?? '';
    return Response.json({ ok: true });
  };
  try {
    setCsrfToken('stale-token');
    await fetchApi('/auth/local/login', {
      method: 'POST', headers: { 'X-CSRF-Token': 'fresh-token' },
    });
    assert.equal(received, 'fresh-token');
  } finally {
    setCsrfToken(null);
    globalThis.fetch = originalFetch;
  }
});