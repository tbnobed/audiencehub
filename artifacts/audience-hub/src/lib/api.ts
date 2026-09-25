export let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiTimeoutError extends Error {
  constructor() {
    super('The request timed out after 15 seconds. Please try again.');
    this.name = 'ApiTimeoutError';
  }
}

type ApiRequestOptions = RequestInit & { timeoutMs?: number };
export const API_TIMEOUT_MS = 15_000;

export async function fetchApi(url: string, options: ApiRequestOptions = {}) {
  // Bound dashboard reads without changing long-running upload/validation writes.
  const { timeoutMs = url.startsWith('/api/dashboards/') ? API_TIMEOUT_MS : undefined, signal, ...requestOptions } = options;
  if (signal?.aborted) throw new DOMException('The request was cancelled.', 'AbortError');
  const headers = new Headers(options.headers || {});
  
  if (options.method && options.method !== 'GET' && options.method !== 'HEAD') {
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken);
    }
  }

  // Ensure same-origin and credentials
  const fetchOptions: RequestInit = {
    ...requestOptions,
    headers,
    credentials: 'include',
  };

  // Race the request against the abort event as well: a stalled response body
  // (or a fetch implementation that ignores abort) must not leave the UI pending.
  const controller = new AbortController();
  fetchOptions.signal = controller.signal;
  let timedOut = false;
  let rejectAbort!: (reason: Error) => void;
  const aborted = new Promise<never>((_, reject) => { rejectAbort = reject; });
  const onAbort = () => {
    controller.abort();
    rejectAbort(timedOut ? new ApiTimeoutError() : new DOMException('The request was cancelled.', 'AbortError'));
  };
  signal?.addEventListener('abort', onAbort, { once: true });
  const timer = timeoutMs === undefined ? undefined : setTimeout(() => { timedOut = true; onAbort(); }, timeoutMs);
  // The signal may have changed between the first check and listener registration.
  if (signal?.aborted) onAbort();

  try {
    return await Promise.race([(async () => {
      const response = await fetch(url, fetchOptions);
      if (!response.ok) {
        let message = 'API Error';
        try {
          const data = await response.json();
          message = data.error?.message || data.message || message;
        } catch {
          // Ignore JSON parse errors for non-JSON responses
        }
        throw new Error(message);
      }
      return response.json();
    })(), aborted]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}
