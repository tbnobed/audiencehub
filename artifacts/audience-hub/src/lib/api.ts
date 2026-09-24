export let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export async function fetchApi(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers || {});
  
  if (options.method && options.method !== 'GET' && options.method !== 'HEAD') {
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken);
    }
  }

  // Ensure same-origin and credentials
  const fetchOptions: RequestInit = {
    ...options,
    headers,
    credentials: 'include',
  };

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
}
