import { useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, FileUp, Loader2, Play, RefreshCw, ServerCog } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import { fetchApi } from '@/lib/api';
import { useSources } from '@/hooks/use-sources';
import { useAuth } from '@/hooks/use-auth';
import {
  ZETA_TARGETS, uploadZetaFile, useCreateZetaSession, useRunZeta, useZetaDropFiles, useZetaMeta, useZetaParity, useZetaSession, useZetaSessions, useZetaSftp,
  type AudienceCounts, type ZetaSession,
} from '@/hooks/use-zeta-migrations';

const selectCls = 'h-9 w-full rounded-md border border-input bg-background px-2 text-sm';
const bytes = (n: number) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(2)} GiB` : n >= 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : `${Math.ceil(n / 1024)} KiB`);
const statusCls = (s: string) => s === 'completed' || s === 'complete' ? 'bg-ok/15 text-ok' : s === 'failed' ? 'bg-destructive/15 text-destructive' : 'bg-muted text-muted-foreground';

export function ZetaImportButton() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  if (user?.role !== 'admin') return null;
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)} data-testid="button-import-zeta"><ServerCog className="mr-2 h-4 w-4" /> Import Zeta export</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-4xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>Zeta export migration</DialogTitle>
            <DialogDescription>Resumable upload or SFTP drop, automatic file detection, then parity against Zeta counts.</DialogDescription>
          </DialogHeader>
          {open && <ZetaImport />}
        </DialogContent>
      </Dialog>
    </>
  );
}

function CountsEditor({ counts, setCounts, audiences }: { counts: AudienceCounts; setCounts: (c: AudienceCounts) => void; audiences: string[] }) {
  return (
    <div className="grid max-h-64 gap-1.5 overflow-auto sm:grid-cols-2" data-testid="editor-audience-counts">
      {audiences.map(a => (
        <label key={a} className="flex items-center gap-2 rounded border bg-background px-2 py-1 text-xs">
          <span className="flex-1 truncate" title={a}>{a}</span>
          <Input type="number" min={0} step={1} className="h-7 w-28 text-right font-mono text-xs" value={counts[a] ?? 0} data-testid={`input-zeta-count-${a}`}
            onChange={e => setCounts({ ...counts, [a]: Math.max(0, Math.floor(Number(e.target.value) || 0)) })} />
        </label>
      ))}
    </div>
  );
}

function ZetaImport() {
  const [sessionId, setSessionId] = useState<number | string | null>(null);
  const sessions = useZetaSessions();
  if (sessionId !== null) return <SessionView id={sessionId} onBack={() => setSessionId(null)} />;
  return <SessionPicker sessions={sessions} onOpen={setSessionId} />;
}

function SessionPicker({ sessions, onOpen }: { sessions: ReturnType<typeof useZetaSessions>; onOpen: (id: number | string) => void }) {
  const { data: src } = useSources();
  const meta = useZetaMeta();
  const create = useCreateZetaSession();
  const eligible = (src?.items ?? []).filter(s => s.kind === 'zeta_exit' || s.kind === 'sftp_jsonl');
  const [sourceId, setSourceId] = useState('');
  const [name, setName] = useState('');
  const [edited, setEdited] = useState<AudienceCounts | null>(null);
  const audiences = meta.data?.audiences ?? [];
  const counts = useMemo<AudienceCounts>(() => edited ?? Object.fromEntries(audiences.map(a => [a.name, a.zeta_count])), [edited, audiences]);
  const submit = () => create.mutate({ source_id: Number(sourceId), name: name.trim() || undefined, audience_counts: counts }, { onSuccess: s => onOpen(s.id) });
  return (
    <div className="space-y-6 text-sm">
      <section className="space-y-2">
        <h3 className="kin-heading">Sessions</h3>
        {sessions.isLoading ? <div className="h-12 animate-pulse rounded-md bg-muted" /> : sessions.error ? (
          <div role="alert" className="flex items-center gap-2 text-xs text-destructive" data-testid="text-zeta-sessions-error">{sessions.error.message}
            <Button size="sm" variant="ghost" onClick={() => sessions.refetch()}>Retry</Button></div>
        ) : (sessions.data?.items ?? []).length === 0 ? (
          <p className="rounded-md border border-dashed p-3 text-center text-xs text-muted-foreground" data-testid="empty-zeta-sessions">No migration sessions yet.</p>
        ) : (
          <div className="divide-y rounded-md border">
            {sessions.data!.items.map(s => (
              <button key={s.id} className="flex w-full items-center gap-3 px-3 py-2 text-left text-xs hover:bg-muted/50" onClick={() => onOpen(s.id)} data-testid={`row-zeta-session-${s.id}`}>
                <span className="font-mono">#{s.id}</span><span className="flex-1 truncate">{s.name || 'Untitled migration'}</span>
                <span className="text-muted-foreground">{s.files.length} files</span>
                <span className={`rounded px-1.5 text-[10px] uppercase ${statusCls(s.status)}`}>{s.status}</span>
              </button>
            ))}
          </div>
        )}
      </section>
      <section className="space-y-3 border-t pt-4">
        <h3 className="kin-heading">New session</h3>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">Zeta exit / SFTP source</Label>
            <select className={selectCls} value={sourceId} onChange={e => setSourceId(e.target.value)} data-testid="select-zeta-source">
              <option value="">Choose a source</option>
              {eligible.map(s => <option key={s.id} value={s.id}>{s.name} ({s.kind})</option>)}
            </select>
            {eligible.length === 0 && <p className="text-[11px] text-warn" data-testid="text-zeta-no-source">No zeta_exit or sftp_jsonl source exists. Create one in Sources first.</p>}
          </div>
          <div className="space-y-1"><Label className="text-xs">Name (optional)</Label>
            <Input value={name} onChange={e => setName(e.target.value)} placeholder="Zeta final export" data-testid="input-zeta-name" /></div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Zeta audience counts ({audiences.length} catalog entries). Used for parity.</Label>
          {meta.isLoading ? <div className="h-24 animate-pulse rounded-md bg-muted" /> : meta.error ? (
            <p role="alert" className="text-xs text-destructive" data-testid="text-zeta-meta-error">Unable to load audience catalog: {meta.error.message}</p>
          ) : <CountsEditor counts={counts} setCounts={setEdited} audiences={audiences.map(a => a.name)} />}
        </div>
        {create.error && <p role="alert" className="text-xs text-destructive" data-testid="text-zeta-create-error">{create.error.message}</p>}
        <div className="flex justify-end">
          <Button disabled={!sourceId || audiences.length === 0 || create.isPending} onClick={submit} data-testid="button-create-zeta-session">
            {create.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />} Create session
          </Button>
        </div>
      </section>
    </div>
  );
}

type UploadState = { name: string; offset: number; size: number; error?: string; done?: boolean };

function SessionView({ id, onBack }: { id: number | string; onBack: () => void }) {
  const q = useZetaSession(id);
  const client = useQueryClient();
  const run = useRunZeta(id);
  const sftp = useZetaSftp(id);
  const parity = useZetaParity(id);
  const meta = useZetaMeta();
  const [target, setTarget] = useState('auto');
  const [uploads, setUploads] = useState<Record<string, UploadState>>({});
  const [uploading, setUploading] = useState(false);
  const [showDrop, setShowDrop] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [parityCounts, setParityCounts] = useState<AudienceCounts | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const drop = useZetaDropFiles(id, showDrop);
  const s: ZetaSession | undefined = q.data;
  const audiences = meta.data?.audiences ?? [];
  const counts = parityCounts ?? Object.fromEntries(audiences.map(a => [a.name, a.zeta_count]));

  const onFiles = async (list: FileList | null) => {
    if (!list || !s) return;
    const files = Array.from(list);
    abortRef.current = new AbortController();
    setUploading(true);
    for (const file of files) {
      const key = `${file.name}:${file.size}`;
      setUploads(u => ({ ...u, [key]: { name: file.name, offset: 0, size: file.size } }));
      try {
        const latest = (await client.fetchQuery({ queryKey: ['zeta-migrations', String(id)], staleTime: 0, queryFn: () => fetchApi(`/api/zeta-migrations/${id}`) })) as ZetaSession;
        await uploadZetaFile({ sessionId: id, file, targetType: target, existing: latest.files, signal: abortRef.current.signal,
          onProgress: (offset, size) => setUploads(u => ({ ...u, [key]: { name: file.name, offset, size } })) });
        setUploads(u => ({ ...u, [key]: { name: file.name, offset: file.size, size: file.size, done: true } }));
      } catch (e) {
        setUploads(u => ({ ...u, [key]: { name: file.name, offset: u[key]?.offset ?? 0, size: file.size, error: e instanceof Error ? e.message : String(e) } }));
      }
      client.invalidateQueries({ queryKey: ['zeta-migrations'] });
    }
    setUploading(false);
  };

  if (q.isLoading) return <div className="space-y-2" role="status">{[0, 1, 2].map(i => <div key={i} className="h-10 animate-pulse rounded-md bg-muted" />)}</div>;
  if (q.error || !s) return (
    <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm" data-testid="text-zeta-session-error">
      <p className="text-destructive">Unable to load session: {q.error?.message}</p>
      <div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => q.refetch()}><RefreshCw className="mr-2 h-3.5 w-3.5" /> Retry</Button><Button size="sm" variant="ghost" onClick={onBack}>Back</Button></div>
    </div>
  );

  const pr = s.progress;
  const pct = pr?.total ? Math.min(100, ((pr.done ?? 0) / pr.total) * 100) : 0;
  const busy = ['queued', 'running', 'importing', 'processing', 'downloading'].includes(s.status);
  const par = s.parity;
  const parActive = !!par && ['waiting', 'queued', 'running'].includes(par.status);

  return (
    <div className="space-y-6 text-sm" data-testid="zeta-session">
      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" variant="ghost" onClick={onBack} data-testid="button-zeta-back">Sessions</Button>
        <span className="font-medium">{s.name || 'Untitled migration'}</span>
        <span className="font-mono text-xs text-muted-foreground">#{s.id}</span>
        <Badge variant="outline" className={`uppercase text-[10px] ${statusCls(s.status)}`} data-testid="status-zeta-session">{s.status}</Badge>
        {s.batch_id != null && <span className="text-xs text-muted-foreground">batch {s.batch_id}</span>}
        {s.job_id != null && <span className="text-xs text-muted-foreground">job #{s.job_id}</span>}
      </div>

      {(busy || pr) && (
        <div className="space-y-1 rounded-md border p-3" data-testid="status-zeta-progress">
          <p className="text-xs">{pr?.message || pr?.phase || 'Working…'}</p>
          <Progress value={pct} className="h-1.5" />
          {pr?.total ? <p className="font-mono text-[10px] text-muted-foreground">{(pr.done ?? 0).toLocaleString()} / {pr.total.toLocaleString()}</p> : null}
        </div>
      )}
      {s.error && (
        <div role="alert" className="flex gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive" data-testid="text-zeta-error">
          <AlertTriangle className="h-4 w-4 shrink-0" /> {s.error}
        </div>
      )}

      <section className="space-y-3">
        <h3 className="kin-heading">Files</h3>
        {s.files.length === 0 ? <p className="rounded-md border border-dashed p-3 text-center text-xs text-muted-foreground" data-testid="empty-zeta-files">No files yet. Upload an export or pick files from the SFTP drop.</p> : (
          <div className="divide-y rounded-md border">
            {s.files.map(f => (
              <div key={f.id} className="space-y-1 px-3 py-2 text-xs" data-testid={`row-zeta-file-${f.id}`}>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="flex-1 truncate font-mono">{f.filename}</span>
                  <span className="text-muted-foreground">{f.target_type ?? 'auto'}</span>
                  <span className="text-muted-foreground">{bytes(f.offset)} / {bytes(f.size)}</span>
                  <span className={`rounded px-1.5 text-[10px] uppercase ${statusCls(f.status)}`}>{f.status}</span>
                </div>
                {f.offset < f.size && <Progress value={f.size ? (f.offset / f.size) * 100 : 0} className="h-1" />}
              </div>
            ))}
          </div>
        )}
        <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-3 items-end">
          <div className="space-y-1">
            <Label className="text-xs">File type</Label>
            <select className={selectCls} value={target} onChange={e => setTarget(e.target.value)} data-testid="select-zeta-file-type">
              {ZETA_TARGETS.map(t => <option key={t} value={t}>{t === 'auto' ? 'auto-detect' : t}</option>)}
            </select>
            <p className="text-[10px] text-muted-foreground">Override only if detection is ambiguous.</p>
          </div>
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="zeta-file" className="text-xs">Upload (chunked, resumable). After a refresh, select the same file to resume.</Label>
            <Input id="zeta-file" type="file" multiple disabled={uploading} className="h-9 text-xs" data-testid="input-zeta-file" onChange={e => { onFiles(e.target.files); e.target.value = ''; }} />
          </div>
        </div>
        {Object.entries(uploads).map(([k, u]) => (
          <div key={k} className="space-y-1 text-xs" data-testid={`status-zeta-upload-${u.name}`}>
            <div className="flex items-center gap-2"><FileUp className="h-3.5 w-3.5" /><span className="flex-1 truncate font-mono">{u.name}</span>
              <span className="text-muted-foreground">{bytes(u.offset)} / {bytes(u.size)}</span>{u.done && <CheckCircle2 className="h-3.5 w-3.5 text-ok" />}</div>
            <Progress value={u.size ? (u.offset / u.size) * 100 : 100} className="h-1" />
            {u.error && <p role="alert" className="text-destructive">{u.error}</p>}
          </div>
        ))}
        {uploading && <Button size="sm" variant="outline" onClick={() => abortRef.current?.abort()} data-testid="button-cancel-upload">Pause upload</Button>}
      </section>

      <section className="space-y-2 border-t pt-4">
        <div className="flex items-center gap-2">
          <h3 className="kin-heading mr-auto">SFTP drop</h3>
          <Button size="sm" variant="outline" onClick={() => { setShowDrop(true); if (showDrop) drop.refetch(); }} data-testid="button-list-drop">{showDrop ? 'Refresh' : 'List drop files'}</Button>
        </div>
        {showDrop && (drop.isLoading ? <div className="h-12 animate-pulse rounded-md bg-muted" /> : drop.error ? (
          <p role="alert" className="text-xs text-destructive" data-testid="text-drop-error">{drop.error.message}</p>
        ) : (drop.data?.items ?? []).length === 0 ? <p className="text-xs text-muted-foreground" data-testid="empty-drop">No files in the configured drop.</p> : (
          <>
            <div className="max-h-48 divide-y overflow-auto rounded-md border">
              {drop.data!.items.map(f => (
                <label key={f.path} className="flex items-center gap-3 px-3 py-1.5 text-xs">
                  <input type="checkbox" checked={picked.includes(f.path)} data-testid={`check-drop-${f.path}`}
                    onChange={e => setPicked(p => (e.target.checked ? [...p, f.path] : p.filter(x => x !== f.path)))} />
                  <span className="flex-1 truncate font-mono">{f.path}</span><span className="text-muted-foreground">{bytes(f.size)}</span>
                  <span className="text-muted-foreground">{new Date(f.modified).toLocaleDateString()}</span>
                </label>
              ))}
            </div>
            <Button size="sm" disabled={picked.length === 0 || sftp.isPending || busy} data-testid="button-start-sftp" onClick={() => sftp.mutate(picked, { onSuccess: () => setPicked([]) })}>
              {sftp.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />} Download and import {picked.length || ''} selected
            </Button>
          </>
        ))}
        {sftp.error && <p role="alert" className="text-xs text-destructive" data-testid="text-sftp-error">{sftp.error.message}</p>}
      </section>

      <section className="flex flex-wrap items-center gap-3 border-t pt-4">
        <Button disabled={run.isPending || busy || s.files.length === 0 || uploading} onClick={() => run.mutate()} data-testid="button-run-zeta">
          {run.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : s.status === 'failed' ? <RefreshCw className="mr-2 h-4 w-4" /> : <Play className="mr-2 h-4 w-4" />}
          {s.status === 'failed' ? 'Retry run' : 'Run import'}
        </Button>
        {run.error && <p role="alert" className="text-xs text-destructive" data-testid="text-run-error">{run.error.message}</p>}
        <span className="text-[11px] text-muted-foreground">Import progress also appears in the table above.</span>
      </section>

      <section className="space-y-3 border-t pt-4">
        <h3 className="kin-heading">Parity vs Zeta</h3>
        <details className="rounded-md border p-2"><summary className="cursor-pointer text-xs">Zeta counts ({audiences.length})</summary>
          <div className="mt-2"><CountsEditor counts={counts} setCounts={setParityCounts} audiences={audiences.map(a => a.name)} /></div></details>
        <Button size="sm" variant="outline" disabled={parity.isPending || parActive || audiences.length === 0} onClick={() => parity.mutate(counts)} data-testid="button-run-parity">
          {(parity.isPending || parActive) && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />} {parActive ? 'Parity running' : 'Run parity'}
        </Button>
        {parity.error && <p role="alert" className="text-xs text-destructive" data-testid="text-parity-error">{parity.error.message}</p>}
        {par?.status === 'waiting' && <p className="text-xs text-muted-foreground" data-testid="status-parity-waiting">Waiting for identity and trait processing to complete.</p>}
        {par?.error && <p role="alert" className="text-xs text-destructive">{par.error}</p>}
        {par?.items && par.items.length > 0 && (
          <div className="max-h-96 overflow-auto rounded-md border" data-testid="table-parity">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-muted"><tr><th className="px-3 py-1.5 text-left">Audience</th><th className="px-3 py-1.5 text-right">Kinship</th><th className="px-3 py-1.5 text-right">Zeta</th><th className="px-3 py-1.5 text-right">Diff</th><th className="px-3 py-1.5 text-right">%</th></tr></thead>
              <tbody>
                {par.items.map(r => (
                  <tr key={r.name} className="border-t" data-testid={`row-parity-${r.name}`}>
                    <td className="px-3 py-1">{r.name}</td>
                    <td className="px-3 py-1 text-right font-mono">{r.kinship_count.toLocaleString()}</td>
                    <td className="px-3 py-1 text-right font-mono">{r.zeta_count.toLocaleString()}</td>
                    <td className={`px-3 py-1 text-right font-mono ${r.difference ? 'text-destructive' : 'text-ok'}`}>{r.difference > 0 ? '+' : ''}{r.difference.toLocaleString()}</td>
                    <td className="px-3 py-1 text-right font-mono">{r.percentage === null ? 'n/a' : `${r.percentage.toFixed(2)}%`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
