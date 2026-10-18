import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export const ZETA_TARGETS = ['auto', 'people', 'donation', 'donation_return', 'order_shipped', 'vod_viewed', 'source_codes', 'products', 'vod_titles', 'model_scores', 'lookups'] as const;
export const CHUNK_BYTES = 8 * 1024 * 1024;

export type ZetaFile = { id: number | string; filename: string; size: number; offset: number; status: string; target_type?: string | null; fingerprint?: string | null };
export type ZetaParityItem = { name: string; kinship_count: number; zeta_count: number; difference: number; percentage: number | null };
export type ZetaSession = {
  id: number | string; source_id: number; name?: string | null; status: string; job_id?: number | null; batch_id?: number | string | null;
  files: ZetaFile[];
  progress?: { message?: string; phase?: string; done?: number; total?: number } | null;
  error?: string | null;
  parity?: { status: string; items?: ZetaParityItem[]; error?: string | null } | null;
};
export type AudienceCounts = Record<string, number>;
export type JobResult = { job_id: number | string; status: string };

const post = (url: string, body: unknown) => fetchApi(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const sessionKey = (id: number | string) => ['zeta-migrations', String(id)] as const;

export function useZetaMeta(enabled = true) {
  return useQuery<{ audiences: { name: string; zeta_count: number }[] }>({
    queryKey: ['zeta-migrations', 'meta'], enabled, retry: false, staleTime: 300_000,
    queryFn: ({ signal }) => fetchApi('/api/zeta-migrations/meta', { signal, timeoutMs: 15000 }),
  });
}
export function useZetaSessions(enabled = true) {
  return useQuery<{ items: ZetaSession[] }>({
    queryKey: ['zeta-migrations', 'list'], enabled, retry: false,
    queryFn: ({ signal }) => fetchApi('/api/zeta-migrations', { signal, timeoutMs: 15000 }),
  });
}
const PARITY_ACTIVE = ['waiting', 'queued', 'running'];
export const sessionActive = (s?: ZetaSession) =>
  !!s && (['queued', 'running', 'importing', 'processing', 'downloading'].includes(s.status) || (!!s.parity && PARITY_ACTIVE.includes(s.parity.status)));
export function useZetaSession(id: number | string | null) {
  return useQuery<ZetaSession>({
    queryKey: sessionKey(id ?? 'none'), enabled: id !== null, retry: false,
    queryFn: ({ signal }) => fetchApi(`/api/zeta-migrations/${id}`, { signal, timeoutMs: 15000 }),
    refetchInterval: q => (sessionActive(q.state.data) ? 3000 : false),
  });
}
export function useZetaDropFiles(id: number | string, enabled: boolean) {
  return useQuery<{ items: { path: string; size: number; modified: string }[] }>({
    queryKey: [...sessionKey(id), 'drop'], enabled, retry: false, gcTime: 0,
    queryFn: ({ signal }) => fetchApi(`/api/zeta-migrations/${id}/drop-files`, { signal, timeoutMs: 30000 }),
  });
}

function useSessionMutation<V, R>(fn: (v: V) => Promise<R>, id?: number | string) {
  const client = useQueryClient();
  return useMutation<R, Error, V>({
    mutationFn: fn,
    onSettled: () => {
      client.invalidateQueries({ queryKey: ['zeta-migrations'] });
      client.invalidateQueries({ queryKey: ['imports'] });
      void id;
    },
  });
}
export const useCreateZetaSession = () =>
  useSessionMutation<{ source_id: number; name?: string; audience_counts: AudienceCounts }, ZetaSession>(b => post('/api/zeta-migrations', b));
export const useRunZeta = (id: number | string) => useSessionMutation<void, JobResult>(() => post(`/api/zeta-migrations/${id}/run`, {}), id);
export const useZetaSftp = (id: number | string) => useSessionMutation<string[], JobResult>(paths => post(`/api/zeta-migrations/${id}/sftp`, { paths }), id);
export const useZetaParity = (id: number | string) => useSessionMutation<AudienceCounts, JobResult>(c => post(`/api/zeta-migrations/${id}/parity`, { audience_counts: c }), id);

const hex = (b: ArrayBuffer) => Array.from(new Uint8Array(b)).map(x => x.toString(16).padStart(2, '0')).join('');
/** Lightweight identity: name + size + first and last 64KiB. Never reads the whole file. */
export async function fingerprintFile(file: File): Promise<string> {
  const edge = 64 * 1024;
  const head = await file.slice(0, Math.min(edge, file.size)).arrayBuffer();
  const tail = file.size > edge ? await file.slice(Math.max(edge, file.size - edge)).arrayBuffer() : new ArrayBuffer(0);
  const meta = new TextEncoder().encode(`${file.name}|${file.size}|`);
  const buf = new Uint8Array(meta.length + head.byteLength + tail.byteLength);
  buf.set(meta, 0); buf.set(new Uint8Array(head), meta.length); buf.set(new Uint8Array(tail), meta.length + head.byteLength);
  return hex(await crypto.subtle.digest('SHA-256', buf));
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

/** Resumable chunk upload. Offsets are authoritative on the server; failures re-read the session. */
export async function uploadZetaFile(opts: {
  sessionId: number | string; file: File; targetType: string; existing: ZetaFile[];
  onProgress: (offset: number, size: number) => void; signal?: AbortSignal;
}): Promise<void> {
  const { sessionId, file, targetType, existing, onProgress, signal } = opts;
  const fingerprint = await fingerprintFile(file);
  let rec = existing.find(f => f.filename === file.name && f.size === file.size && (!f.fingerprint || f.fingerprint === fingerprint) && f.status !== 'completed');
  if (!rec) {
    const body: Record<string, unknown> = { filename: file.name, size: file.size, fingerprint };
    if (targetType && targetType !== 'auto') body.target_type = targetType; else body.target_type = 'auto';
    rec = await post(`/api/zeta-migrations/${sessionId}/files`, body) as ZetaFile;
  }
  let offset = rec.offset ?? 0;
  const fileId = rec.id;
  onProgress(offset, file.size);
  let failures = 0;
  while (offset < file.size) {
    if (signal?.aborted) throw new DOMException('Upload cancelled.', 'AbortError');
    const end = Math.min(offset + CHUNK_BYTES, file.size);
    try {
      const res = await fetchApi(`/api/zeta-migrations/${sessionId}/files/${fileId}`, {
        method: 'PATCH', headers: { 'Upload-Offset': String(offset), 'Content-Type': 'application/octet-stream' }, body: file.slice(offset, end), signal,
      }) as { offset: number };
      if (typeof res.offset !== 'number' || res.offset <= offset) throw new Error('Server did not advance the upload offset.');
      offset = res.offset; failures = 0;
      onProgress(offset, file.size);
    } catch (e) {
      if (signal?.aborted || (e instanceof DOMException && e.name === 'AbortError')) throw e;
      if (++failures > 5) throw new Error(`Upload of ${file.name} stopped at ${offset.toLocaleString()} of ${file.size.toLocaleString()} bytes: ${e instanceof Error ? e.message : String(e)}. Select the same file again to resume.`);
      await sleep(Math.min(8000, 500 * 2 ** failures));
      try {
        const s = await fetchApi(`/api/zeta-migrations/${sessionId}`) as ZetaSession;
        const f = s.files.find(x => String(x.id) === String(fileId));
        if (f) offset = f.offset;
      } catch { /* retry loop continues with prior offset */ }
    }
  }
}
