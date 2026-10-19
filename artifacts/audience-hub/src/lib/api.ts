export let csrfToken: string | null = null;
let lastInteraction = Date.now();
if (typeof window !== 'undefined') {
  for (const event of ['pointerdown', 'keydown', 'touchstart']) {
    window.addEventListener(event, () => { lastInteraction = Date.now(); }, { passive: true });
  }
}

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiTimeoutError extends Error {
  constructor() {
    super('The request timed out after 15 seconds. Please try again.');
    this.name = 'ApiTimeoutError';
  }
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

type ApiRequestOptions = RequestInit & { timeoutMs?: number };
export const API_TIMEOUT_MS = 15_000;

export function apiUrl(path: string, baseUrl = import.meta.env?.BASE_URL ?? '/') {
  return path.startsWith('/api/')
    ? `${baseUrl.replace(/\/$/, '')}${path}`
    : path;
}

export async function fetchApi(url: string, options: ApiRequestOptions = {}) {
  // Bound dashboard reads without changing long-running upload/validation writes.
  const { timeoutMs = url.startsWith('/api/dashboards/') ? API_TIMEOUT_MS : undefined, signal, ...requestOptions } = options;
  if (signal?.aborted) throw new DOMException('The request was cancelled.', 'AbortError');
  const headers = new Headers(options.headers || {});
  // Background polling must not keep an unattended session alive forever.
  headers.set('X-Kinship-Activity', Date.now() - lastInteraction < 60_000 ? '1' : '0');
  
  if (options.method && options.method !== 'GET' && options.method !== 'HEAD') {
    if (csrfToken && !headers.has('X-CSRF-Token')) {
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
      const response = await fetch(apiUrl(url), fetchOptions);
      if (!response.ok) {
        // The session query handles its own expected anonymous response.
        // Dispatching expiration from it can cancel/restart that same query.
        if (response.status === 401 && url.startsWith('/api/') && url !== '/api/me' && typeof window !== 'undefined') {
          window.dispatchEvent(new Event('kinship-session-expired'));
        }
        let message = 'API Error';
        try {
          const data = await response.json();
          message = data.error?.message || data.message || message;
        } catch {
          // Ignore JSON parse errors for non-JSON responses
        }
        throw new ApiError(response.status, message);
      }
      return response.json();
    })(), aborted]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}
