// Run: node --test src/lib/api.test.ts (Node >= 22.18)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiTimeoutError, fetchApi } from './api.ts';

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