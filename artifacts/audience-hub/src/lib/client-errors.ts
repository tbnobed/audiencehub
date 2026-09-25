import type { ErrorInfo } from 'react';
import { fetchApi } from './api';

// Closed vocabulary: never send error messages, arbitrary display names, or URLs.
const components = new Set(['App', 'ErrorBoundary', 'ProfileDetail', 'ProfileSectionBoundary', 'Overview', 'System', 'Card', 'CardContent', 'Table', 'TableBody', 'TableRow', 'TableCell']);
const routes = new Set(['/', '/profiles', '/profiles/:id', '/system', '/sources', '/imports', '/settings', '/data-health']);
export async function reportClientError(error: unknown, info: Pick<ErrorInfo, 'componentStack'>, route: string, profileId?: number | string): Promise<void> {
  try {
    const name = error instanceof Error ? error.name : '';
    const category = ['TypeError', 'RangeError', 'ReferenceError', 'SyntaxError'].includes(name) ? name : 'RenderError';
    const path = route.split(/[?#]/, 1)[0];
    const safeRoute = routes.has(path) || /^\/profiles\/[0-9]{1,15}$/.test(path) ? path : '/unknown';
    const stack = (info.componentStack ?? '').slice(0, 8192).split('\n')
      .map(line => /^\s*(?:at|in)\s+([A-Za-z][A-Za-z0-9]*)\b/.exec(line)?.[1])
      .filter((name): name is string => !!name && components.has(name)).slice(0, 16);
    const id = typeof profileId === 'string' && /^[0-9]{1,15}$/.test(profileId) ? Number(profileId) : profileId;
    await fetchApi('/api/client-errors', {
      method: 'POST', timeoutMs: 5000, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, route: safeRoute, component_stack: stack,
        profile_id: typeof id === 'number' && Number.isSafeInteger(id) && id > 0 && id <= 999999999999999 ? id : null }),
    });
  } catch {
    // Reporting is deliberately best effort, never log/rethrow or recursively report.
  }
}