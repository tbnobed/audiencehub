import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, CheckCircle2, KeyRound, Play, Plus, RefreshCw, ScanSearch, Trash2, Wand2, Plug } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import type { Source } from '@/hooks/use-sources';
import {
  FEEDS, RESOURCE_TYPES, TARGET_TYPES, deriveFeed, type MappingSchedule, type TargetType, SFTP_FOLDERS, type ConnectionConfig, type Feed, type FeedMapping, type FeedSchedule, type PullHistoryItem, ACTIVE_PULL_STATUSES,
  useSaveSourceConnection, useSourceTest, useSourceColumns, useSourceConnection, useSourceDryRun, useSourcePull, useSourcePulls,
} from '@/hooks/use-source-connection';

type CredField = { key: string; label: string; secret?: boolean; multiline?: boolean; required?: boolean; numeric?: boolean };
const SFTP_CREDS: CredField[] = [
  { key: 'host', label: 'Host', required: true },
  { key: 'port', label: 'Port', numeric: true },
  { key: 'username', label: 'Username', required: true },
  { key: 'host_key_sha256', label: 'Host key SHA-256' },
  { key: 'private_key', label: 'Private key (PEM)', secret: true, multiline: true, required: true },
  { key: 'private_key_passphrase', label: 'Key passphrase', secret: true },
];
const SNOWFLAKE_CREDS: CredField[] = [
  { key: 'account', label: 'Account', required: true },
  { key: 'user', label: 'User', required: true },
  { key: 'role', label: 'Read-only role', required: true },
  { key: 'warehouse', label: 'Warehouse', required: true },
  { key: 'database', label: 'Database', required: true },
  { key: 'schema', label: 'Schema', required: true },
  { key: 'private_key', label: 'Private key (PEM)', secret: true, multiline: true, required: true },
  { key: 'private_key_passphrase', label: 'Key passphrase', secret: true },
];

const ID_RE = /^[a-z][a-z0-9_]{0,63}$/;
const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$/;

/** Client-side mirror of validate_config; the server remains authoritative. */
export function validateConnection(kind: string, config: ConnectionConfig): string[] {
  const errors: string[] = [];
  if (config.mappings.length > 50) errors.push('Configure at most 50 feed mappings.');
  const seen = new Set<string>();
  config.mappings.forEach((m, i) => {
    const n = `Mapping ${i + 1}`;
    if (!ID_RE.test(m.id)) errors.push(`${n}: ID must be a lowercase identifier.`);
    else if (seen.has(m.id)) errors.push(`${n}: ID "${m.id}" is duplicated.`);
    seen.add(m.id);
    if (m.feed === 'resources' && !m.target_type && !RESOURCE_TYPES.includes(m.resource_type as never)) errors.push(`${n}: choose a resource type.`);
    if (kind === 'snowflake') {
      if (!!m.table === !!m.query) errors.push(`${n}: choose exactly one table or query.`);
      if (m.table && !IDENT_RE.test(m.table)) errors.push(`${n}: invalid table identifier.`);
      if (m.query && !/^\s*SELECT\s+/i.test(m.query)) errors.push(`${n}: query must be a restricted SELECT.`);
      if (m.schedule) {
        const sc = m.schedule;
        if (sc.hour !== undefined && (sc.hour < 0 || sc.hour > 23)) errors.push(`${n}: schedule hour must be 0-23.`);
        if (sc.minute !== undefined && (sc.minute < 0 || sc.minute > 59)) errors.push(`${n}: schedule minute must be 0-59.`);
        if (sc.day !== undefined && (sc.day < 1 || sc.day > 28)) errors.push(`${n}: schedule day must be 1-28.`);
      }
      const canon = Object.values(m.columns ?? {}).filter(Boolean);
      if (new Set(canon).size !== canon.length) errors.push(`${n}: two columns map to the same canonical field.`);
      if (m.cursor_column && (!IDENT_RE.test(m.cursor_column) || m.cursor_column.includes('.'))) errors.push(`${n}: cursor must be an unqualified column.`);
    } else {
      if (!SFTP_FOLDERS.includes(m.folder as never)) errors.push(`${n}: choose a Zeta folder.`);
      const p = m.pattern ?? '*.jsonl';
      if (p.length > 180 || p.includes('/') || p.includes('\\')) errors.push(`${n}: invalid filename pattern.`);
    }
  });
  for (const [feed, s] of Object.entries(config.schedules)) {
    if (!s) continue;
    if (s.hour !== undefined && (s.hour < 0 || s.hour > 23)) errors.push(`${feed}: hour must be 0-23.`);
    if (s.day !== undefined && (s.day < 1 || s.day > 28)) errors.push(`${feed}: day must be 1-28.`);
  }
  return errors;
}

/** Drop empty optional fields and fields irrelevant to the kind, so extra="forbid" passes. */
function cleanConfig(kind: string, config: ConnectionConfig): ConnectionConfig {
  const mappings = config.mappings.map(m => {
    const out: FeedMapping = { id: m.id.trim(), feed: m.feed };
    if (kind === 'snowflake') {
      if (m.table?.trim()) out.table = m.table.trim();
      if (m.query?.trim()) out.query = m.query.trim();
      if (m.cursor_column?.trim()) out.cursor_column = m.cursor_column.trim();
      if (m.target_type) out.target_type = m.target_type;
      const cols = Object.fromEntries(Object.entries(m.columns ?? {}).filter(([k, v]) => k && v?.trim()).map(([k, v]) => [k, v.trim()]));
      if (Object.keys(cols).length) out.columns = cols;
      if (m.schedule) {
        const sc: MappingSchedule = { frequency: m.schedule.frequency, timezone: 'America/New_York' };
        if (sc.frequency !== 'manual') sc.minute = m.schedule.minute ?? 0;
        if (sc.frequency === 'daily' || sc.frequency === 'monthly') sc.hour = m.schedule.hour ?? 0;
        if (sc.frequency === 'monthly') sc.day = m.schedule.day ?? 1;
        out.schedule = sc;
      }
    } else {
      out.folder = m.folder;
      out.pattern = m.pattern?.trim() || '*.jsonl';
    }
    if (m.feed === 'resources' && m.resource_type) out.resource_type = m.resource_type;
    return out;
  });
  const schedules: ConnectionConfig['schedules'] = {};
  for (const [feed, s] of Object.entries(config.schedules) as [Feed, FeedSchedule | undefined][]) {
    if (!s) continue;
    const out: FeedSchedule = { frequency: s.frequency, timezone: 'America/New_York' };
    if (s.frequency === 'daily' || s.frequency === 'monthly') out.hour = s.hour ?? 0;
    if (s.frequency === 'monthly') out.day = s.day ?? 1;
    schedules[feed] = out;
  }
  return { mappings, schedules };
}

const blankMapping = (kind: string, n: number): FeedMapping =>
  kind === 'snowflake' ? { id: `feed_${n}`, feed: 'people', table: '' } : { id: `feed_${n}`, feed: 'people', folder: '/users/', pattern: '*.jsonl' };

const selectCls = 'h-8 w-full rounded-md border border-input bg-background px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring';

const fmt = (v: string | null) => (v ? new Date(v).toLocaleString() : '—');
const n = (v: number) => v.toLocaleString();
const statusCls = (s: string) =>
  s === 'completed' ? 'bg-ok/15 text-ok' : s === 'failed' || s === 'dead' ? 'bg-destructive/15 text-destructive' : ACTIVE_PULL_STATUSES.includes(s) ? 'bg-signal-soft text-primary' : 'bg-muted text-muted-foreground';

function PullHistory({ items }: { items: PullHistoryItem[] }) {
  if (items.length === 0) return <p className="rounded-md border border-dashed p-3 text-center text-xs text-muted-foreground" data-testid="empty-pulls">No pulls yet.</p>;
  return (
    <div className="divide-y rounded-md border" data-testid="list-pulls">
      {items.map(p => (
        <div key={p.job_id} className="space-y-1 px-3 py-2 text-xs" data-testid={`row-pull-${p.job_id}`}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-mono">job #{p.job_id}</span>
            <span>{p.feed ?? 'all feeds'}</span>
            <span className={`rounded px-1.5 text-[10px] uppercase ${statusCls(p.status)}`} data-testid={`status-pull-${p.job_id}`}>{p.status}</span>
            <span className="text-muted-foreground">attempt {p.attempts}/{p.max_attempts}</span>
            <span className="ml-auto text-muted-foreground">{fmt(p.created_at)}</span>
          </div>
          <div className="flex flex-wrap gap-x-4 text-[11px] text-muted-foreground" data-testid={`counts-pull-${p.job_id}`}>
            <span>imports {n(p.counts.imports_completed)}/{n(p.counts.imports_total)}</span>
            <span>rows {n(p.counts.rows_total)}</span>
            <span className="text-ok">ok {n(p.counts.rows_ok)}</span>
            <span className={p.counts.rows_rejected ? 'text-destructive' : ''}>rejected {n(p.counts.rows_rejected)}</span>
            {p.started_at && <span>started {fmt(p.started_at)}</span>}
            {p.finished_at && <span>finished {fmt(p.finished_at)}</span>}
            {!p.finished_at && p.heartbeat_at && <span>heartbeat {fmt(p.heartbeat_at)}</span>}
          </div>
          {p.error && <p role="alert" className="text-[11px] text-destructive" data-testid={`error-pull-${p.job_id}`}>{p.error}</p>}
        </div>
      ))}
    </div>
  );
}

const CANON_HINT = 'canonical field (blank keeps as extra attribute)';

function SnowflakeMappingExtras({ m, i, tables, cols, colsError, busy, onChange, onIntrospect }: {
  m: FeedMapping; i: number; tables: string[]; cols?: string[]; colsError?: string; busy: boolean;
  onChange: (patch: Partial<FeedMapping>) => void; onIntrospect: () => void;
}) {
  void tables;
  const headers = Array.from(new Set([...(cols ?? []), ...Object.keys(m.columns ?? {})]));
  const sc = m.schedule;
  const setSc = (patch: Partial<MappingSchedule> | null) => onChange({ schedule: patch === null ? undefined : { ...(sc ?? { frequency: 'manual' }), ...patch, timezone: 'America/New_York' } });
  return (
    <div className="sm:col-span-12 space-y-3 border-t pt-3">
      <div className="grid gap-2 sm:grid-cols-12 items-end">
        <div className="sm:col-span-4 space-y-1">
          <Label className="text-[11px]">Target type</Label>
          <select className={selectCls} value={m.target_type ?? ''} data-testid={`select-mapping-target-${i}`}
            onChange={e => { const t = e.target.value as TargetType | ''; onChange(t ? { target_type: t, ...deriveFeed(t), resource_type: deriveFeed(t).resource_type } : { target_type: undefined }); }}>
            <option value="">Choose feed manually</option>
            {TARGET_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          {m.target_type && <p className="text-[10px] text-muted-foreground">feed: {m.feed}{m.resource_type ? ` / ${m.resource_type}` : ''}</p>}
        </div>
        <div className="sm:col-span-3 space-y-1">
          <Label className="text-[11px]">Schedule</Label>
          <select className={selectCls} value={sc?.frequency ?? ''} data-testid={`select-mapping-schedule-${i}`}
            onChange={e => e.target.value ? setSc({ frequency: e.target.value as MappingSchedule['frequency'] }) : setSc(null)}>
            <option value="">Source default</option>
            {['manual', 'hourly', 'daily', 'monthly'].map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </div>
        {sc && sc.frequency === 'monthly' && <div className="sm:col-span-1 space-y-1"><Label className="text-[11px]">Day</Label>
          <Input type="number" min={1} max={28} className="h-8 text-xs" value={sc.day ?? 1} data-testid={`input-mapping-day-${i}`} onChange={e => setSc({ day: Number(e.target.value) })} /></div>}
        {sc && (sc.frequency === 'daily' || sc.frequency === 'monthly') && <div className="sm:col-span-1 space-y-1"><Label className="text-[11px]">Hour</Label>
          <Input type="number" min={0} max={23} className="h-8 text-xs" value={sc.hour ?? 0} data-testid={`input-mapping-hour-${i}`} onChange={e => setSc({ hour: Number(e.target.value) })} /></div>}
        {sc && sc.frequency !== 'manual' && <div className="sm:col-span-1 space-y-1"><Label className="text-[11px]">Min</Label>
          <Input type="number" min={0} max={59} className="h-8 text-xs" value={sc.minute ?? 0} data-testid={`input-mapping-minute-${i}`} onChange={e => setSc({ minute: Number(e.target.value) })} /></div>}
        {sc && <span className="sm:col-span-2 pb-2 text-[10px] font-mono text-muted-foreground">America/New_York</span>}
      </div>
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-[11px] mr-auto">Column mapping</Label>
          <Button size="sm" variant="outline" className="h-7 text-[11px]" disabled={!m.table?.trim() || busy} onClick={onIntrospect} data-testid={`button-introspect-${i}`}>
            <Wand2 className="mr-1 h-3 w-3" /> {busy ? 'Reading…' : 'Introspect and auto-match'}
          </Button>
        </div>
        {!m.table?.trim() && <p className="text-[10px] text-muted-foreground">Introspection needs a table. For a query, run a dry run and apply suggested columns.</p>}
        {colsError && <p role="alert" className="text-[11px] text-destructive" data-testid={`text-columns-error-${i}`}>{colsError}</p>}
        {headers.length > 0 && (
          <div className="grid max-h-56 gap-1 overflow-auto sm:grid-cols-2">
            {headers.map(h => (
              <div key={h} className="flex items-center gap-2 rounded border bg-background p-1.5">
                <span className="flex-1 truncate font-mono text-[11px]" title={h}>{h}</span>
                <span className="text-muted-foreground">→</span>
                <Input className="h-7 flex-1 font-mono text-[11px]" placeholder={CANON_HINT} value={m.columns?.[h] ?? ''} data-testid={`input-column-${i}-${h}`}
                  onChange={e => onChange({ columns: { ...(m.columns ?? {}), [h]: e.target.value } })} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function SourceConnection({ source }: { source: Source }) {
  const kind = source.kind;
  const credFields = kind === 'snowflake' ? SNOWFLAKE_CREDS : SFTP_CREDS;
  const conn = useSourceConnection(source.id);
  const save = useSaveSourceConnection(source.id);
  const dryRun = useSourceDryRun(source.id);
  const pull = useSourcePull(source.id);
  const pulls = useSourcePulls(source.id);

  const [config, setConfig] = useState<ConnectionConfig>({ mappings: [], schedules: {} });
  const [savedJson, setSavedJson] = useState('');
  const [replaceCreds, setReplaceCreds] = useState(false);
  // Secrets live only in this component's state; cleared on save/unmount, never persisted or logged.
  const [creds, setCreds] = useState<Record<string, string>>({});
  const [mappingFilter, setMappingFilter] = useState('');
  const test = useSourceTest(source.id);
  const columnsApi = useSourceColumns(source.id);
  const [tables, setTables] = useState<string[]>([]);
  const [sourceCols, setSourceCols] = useState<Record<string, string[]>>({});
  const [colsError, setColsError] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const initFor = useRef<number | null>(null);

  useEffect(() => {
    if (conn.data && initFor.current !== source.id) {
      initFor.current = source.id;
      const c = { mappings: conn.data.config.mappings ?? [], schedules: conn.data.config.schedules ?? {} };
      setConfig(c);
      setSavedJson(JSON.stringify(cleanConfig(kind, c)));
      setReplaceCreds(!conn.data.has_credentials);
      const cf = conn.data.connection_fields ?? {};
      setCreds(Object.fromEntries(Object.entries(cf).filter(([k]) => k !== 'private_key' && k !== 'private_key_passphrase').map(([k, v]) => [k, String(v ?? '')])));
    }
  }, [conn.data, source.id, kind]);
  useEffect(() => () => setCreds({}), []);

  const cleaned = useMemo(() => cleanConfig(kind, config), [kind, config]);
  const errors = useMemo(() => validateConnection(kind, config), [kind, config]);
  const credErrors = replaceCreds ? credFields.filter(f => f.required && !creds[f.key]?.trim()).map(f => `${f.label} is required.`) : [];
  const dirty = JSON.stringify(cleaned) !== savedJson || (replaceCreds && credFields.some(f => f.secret && !!creds[f.key]));
  const pending = !!conn.data?.recovery_pending;
  const saved = !!conn.data?.configured;

  const buildCredentials = (): Record<string, unknown> | undefined => {
    if (!replaceCreds) return undefined;
    const out: Record<string, unknown> = {};
    for (const f of credFields) {
      const v = creds[f.key]?.trim();
      if (!v) continue;
      out[f.key] = f.numeric ? Number(v) : f.secret ? creds[f.key] : v;
    }
    return out;
  };

  const handleTest = () => {
    if (replaceCreds && credErrors.length) { setSubmitted(true); return; }
    test.mutate({ credentials: buildCredentials() }, { onSuccess: d => setTables(d.tables ?? []) });
  };

  const introspect = (m: FeedMapping, i: number) => {
    if (!m.table?.trim()) return;
    setColsError(e => ({ ...e, [m.id]: '' }));
    columnsApi.mutate({ table: m.table.trim() }, {
      onSuccess: d => {
        setSourceCols(c => ({ ...c, [m.id]: d.columns }));
        updateMapping(i, { columns: { ...d.suggested_columns, ...Object.fromEntries(Object.entries(m.columns ?? {}).filter(([, v]) => v)) } });
      },
      onError: err => setColsError(e => ({ ...e, [m.id]: err.message })),
    });
  };

  const handleSave = () => {
    setSubmitted(true);
    if (errors.length || credErrors.length || pending) return;
    const credentials = buildCredentials();
    save.mutate({ config: cleaned, credentials }, {
      onSuccess: data => {
        setCreds({});
        setReplaceCreds(false);
        setSubmitted(false);
        const c = { mappings: data.config.mappings ?? [], schedules: data.config.schedules ?? {} };
        setConfig(c);
        setSavedJson(JSON.stringify(cleanConfig(kind, c)));
        dryRun.reset();
      },
    });
  };

  const handlePull = () => {
    pull.mutate({ mapping_id: mappingFilter || null });
  };

  const updateMapping = (i: number, patch: Partial<FeedMapping>) =>
    setConfig(c => ({ ...c, mappings: c.mappings.map((m, j) => (j === i ? { ...m, ...patch } : m)) }));
  const updateSchedule = (feed: Feed, s: FeedSchedule | undefined) =>
    setConfig(c => { const schedules = { ...c.schedules }; if (s) schedules[feed] = s; else delete schedules[feed]; return { ...c, schedules }; });

  if (conn.isLoading) {
    return (
      <div className="space-y-3" data-testid="connection-loading" role="status">
        {[0, 1, 2].map(i => <div key={i} className="h-10 rounded-md bg-muted animate-pulse" />)}
        <span className="sr-only">Loading connection</span>
      </div>
    );
  }
  if (conn.error) {
    return (
      <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm" data-testid="connection-error">
        <p className="font-medium text-destructive">Unable to load connection.</p>
        <p className="text-muted-foreground mt-1">{conn.error.message}</p>
        <Button size="sm" variant="outline" className="mt-3" onClick={() => conn.refetch()} data-testid="button-retry-connection">
          <RefreshCw className="mr-2 h-3.5 w-3.5" /> Retry
        </Button>
      </div>
    );
  }

  const feedsInUse = kind === 'snowflake' ? [] : Array.from(new Set(config.mappings.map(m => m.feed)));
  const shownErrors = submitted ? [...errors, ...credErrors] : [];

  return (
    <div className="space-y-6 text-sm" data-testid="source-connection">
      {pending && (
        <div role="alert" className="flex gap-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-xs" data-testid="status-recovery-pending">
          <AlertTriangle className="h-4 w-4 text-warn shrink-0" />
          A previous pull is pending recovery. Configuration is locked; use Pull now to resume from committed checkpoints.
        </div>
      )}

      {/* Credentials */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="kin-heading flex items-center gap-2"><KeyRound className="h-4 w-4 text-primary" /> Credentials</h3>
          <span className="text-[11px] font-mono text-muted-foreground uppercase">{kind === 'snowflake' ? 'Key-pair auth' : 'SFTP key auth'}</span>
        </div>
        {saved && !replaceCreds ? (
          <div className="flex items-center justify-between rounded-md border bg-muted/40 px-3 py-2" data-testid="status-credentials-saved">
            <div className="flex items-center gap-2 text-xs">
              <CheckCircle2 className="h-4 w-4 text-ok" />
              <span>Encrypted credentials saved</span>
              <span className="font-mono text-muted-foreground tracking-widest">••••••••</span>
            </div>
            <Button size="sm" variant="outline" disabled={pending} onClick={() => setReplaceCreds(true)} data-testid="button-replace-credentials">Replace</Button>
          </div>
        ) : (
          <div className="space-y-3 rounded-md border p-3">
            <p className="text-xs text-muted-foreground">Write-only. Values are sent once over HTTPS, encrypted at rest, and never shown again.</p>
            <div className="grid gap-3 sm:grid-cols-2">
              {credFields.map(f => (
                <div key={f.key} className={f.multiline ? 'space-y-1 sm:col-span-2' : 'space-y-1'}>
                  <Label htmlFor={`cred-${f.key}`} className="text-xs">{f.label}{f.required && <span className="text-destructive"> *</span>}</Label>
                  {f.multiline ? (
                    <Textarea id={`cred-${f.key}`} data-testid={`input-cred-${f.key}`} value={creds[f.key] ?? ''} autoComplete="off" spellCheck={false}
                      className="font-mono text-[11px] h-24 [-webkit-text-security:disc]" placeholder="-----BEGIN PRIVATE KEY-----"
                      onChange={e => setCreds(c => ({ ...c, [f.key]: e.target.value }))} />
                  ) : (
                    <Input id={`cred-${f.key}`} data-testid={`input-cred-${f.key}`} value={creds[f.key] ?? ''} autoComplete={f.secret ? 'new-password' : 'off'}
                      type={f.secret ? 'password' : f.numeric ? 'number' : 'text'} className="h-8 font-mono text-xs"
                      onChange={e => setCreds(c => ({ ...c, [f.key]: e.target.value }))} />
                  )}
                </div>
              ))}
            </div>
            {saved && (
              <Button size="sm" variant="ghost" onClick={() => { setReplaceCreds(false); setCreds({}); }} data-testid="button-keep-credentials">Keep saved credentials</Button>
            )}
          </div>
        )}
        {kind === 'snowflake' && (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" disabled={test.isPending || (!saved && !replaceCreds)} onClick={handleTest} data-testid="button-test-connection">
                <Plug className="mr-1.5 h-3.5 w-3.5" /> {test.isPending ? 'Testing…' : saved && !replaceCreds ? 'Test saved connection' : 'Test connection'}
              </Button>
              {test.data && <span className="text-xs text-ok" data-testid="status-test-ok">Connected. {test.data.version} · {test.data.tables.length.toLocaleString()} tables visible</span>}
            </div>
            {test.error && <p role="alert" className="text-xs text-destructive" data-testid="text-test-error">Connection test failed: {test.error.message}</p>}
          </div>
        )}
      </section>

      <fieldset disabled={pending} className="space-y-6 disabled:opacity-60" data-testid="fieldset-config">
      {/* Mappings */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="kin-heading">Feed mappings</h3>
          <Button size="sm" variant="outline" disabled={pending || config.mappings.length >= 50} data-testid="button-add-mapping"
            onClick={() => setConfig(c => ({ ...c, mappings: [...c.mappings, blankMapping(kind, c.mappings.length + 1)] }))}>
            <Plus className="mr-1 h-3.5 w-3.5" /> Mapping
          </Button>
        </div>
        {config.mappings.length === 0 ? (
          <div className="rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground" data-testid="empty-mappings">
            No feeds mapped. Add one to route {kind === 'snowflake' ? 'a table or query' : 'a Zeta folder'} into a feed.
          </div>
        ) : (
          <div className="space-y-2">
            {config.mappings.map((m, i) => (
              <div key={i} className="grid gap-2 rounded-md border bg-card p-3 sm:grid-cols-12 items-end" data-testid={`row-mapping-${i}`}>
                <div className="sm:col-span-3 space-y-1">
                  <Label className="text-[11px]">ID</Label>
                  <Input className="h-8 font-mono text-xs" value={m.id} data-testid={`input-mapping-id-${i}`} onChange={e => updateMapping(i, { id: e.target.value })} />
                </div>
                <div className="sm:col-span-2 space-y-1">
                  <Label className="text-[11px]">Feed</Label>
                  <select className={selectCls} value={m.feed} data-testid={`select-mapping-feed-${i}`} disabled={kind === 'snowflake' && !!m.target_type} onChange={e => updateMapping(i, { feed: e.target.value as Feed })}>
                    {FEEDS.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
                {kind === 'snowflake' ? (
                  <>
                    <div className="sm:col-span-3 space-y-1">
                      <Label className="text-[11px]">Table</Label>
                      <Input className="h-8 font-mono text-xs" value={m.table ?? ''} placeholder="DB.SCHEMA.TABLE" list="snowflake-tables" data-testid={`input-mapping-table-${i}`}
                        onChange={e => updateMapping(i, { table: e.target.value })} />
                    </div>
                    <div className="sm:col-span-3 space-y-1">
                      <Label className="text-[11px]">Cursor column</Label>
                      <Input className="h-8 font-mono text-xs" value={m.cursor_column ?? ''} placeholder="UPDATED_AT" data-testid={`input-mapping-cursor-${i}`}
                        onChange={e => updateMapping(i, { cursor_column: e.target.value })} />
                    </div>
                  </>
                ) : (
                  <>
                    <div className="sm:col-span-3 space-y-1">
                      <Label className="text-[11px]">Folder</Label>
                      <select className={selectCls} value={m.folder} data-testid={`select-mapping-folder-${i}`} onChange={e => updateMapping(i, { folder: e.target.value })}>
                        {SFTP_FOLDERS.map(f => <option key={f} value={f}>{f}</option>)}
                      </select>
                    </div>
                    <div className="sm:col-span-3 space-y-1">
                      <Label className="text-[11px]">Pattern</Label>
                      <Input className="h-8 font-mono text-xs" value={m.pattern ?? ''} placeholder="*.jsonl" data-testid={`input-mapping-pattern-${i}`}
                        onChange={e => updateMapping(i, { pattern: e.target.value })} />
                    </div>
                  </>
                )}
                <div className="sm:col-span-1 flex justify-end">
                  <Button size="icon" variant="ghost" className="h-8 w-8" aria-label="Remove mapping" data-testid={`button-remove-mapping-${i}`}
                    onClick={() => setConfig(c => ({ ...c, mappings: c.mappings.filter((_, j) => j !== i) }))}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {m.feed === 'resources' && (
                  <div className="sm:col-span-4 space-y-1">
                    <Label className="text-[11px]">Resource type</Label>
                    <select className={selectCls} value={m.resource_type ?? ''} data-testid={`select-mapping-resource-${i}`} onChange={e => updateMapping(i, { resource_type: e.target.value })}>
                      <option value="">Choose…</option>
                      {RESOURCE_TYPES.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </div>
                )}
                {kind === 'snowflake' && (
                  <div className="sm:col-span-12 space-y-1">
                    <Label className="text-[11px]">Or restricted SELECT (no functions, literals or CTEs)</Label>
                    <Textarea className="font-mono text-[11px] h-14" value={m.query ?? ''} data-testid={`input-mapping-query-${i}`}
                      placeholder="SELECT ID, EMAIL, UPDATED_AT FROM DB.SCHEMA.PEOPLE" onChange={e => updateMapping(i, { query: e.target.value })} />
                  </div>
                )}
                {kind === 'snowflake' && (
                  <SnowflakeMappingExtras m={m} i={i} tables={tables} cols={sourceCols[m.id]} colsError={colsError[m.id]} busy={columnsApi.isPending}
                    onChange={patch => updateMapping(i, patch)} onIntrospect={() => introspect(m, i)} />
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Schedules */}
      {kind === 'snowflake' ? <datalist id="snowflake-tables">{tables.map(t => <option key={t} value={t} />)}</datalist> : null}
      {kind !== 'snowflake' && <section className="space-y-3">
        <h3 className="kin-heading">Schedules <span className="text-[11px] font-mono text-muted-foreground font-normal">America/New_York</span></h3>
        {feedsInUse.length === 0 ? (
          <p className="text-xs text-muted-foreground">Schedules appear once a feed is mapped.</p>
        ) : (
          <div className="divide-y rounded-md border">
            {feedsInUse.map(feed => {
              const override = config.schedules[feed];
              const s = override ?? conn.data?.default_schedules[feed] ?? { frequency: 'manual' as const };
              return (
                <div key={feed} className="flex flex-wrap items-center gap-2 px-3 py-2" data-testid={`row-schedule-${feed}`}>
                  <span className="w-24 font-mono text-xs">{feed}</span>
                  <select className={`${selectCls} w-28`} value={s.frequency} data-testid={`select-schedule-${feed}`}
                    onChange={e => updateSchedule(feed, { ...s, frequency: e.target.value as FeedSchedule['frequency'] })}>
                    {['hourly', 'daily', 'monthly', 'manual'].map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                  {s.frequency === 'monthly' && (
                    <label className="flex items-center gap-1 text-xs text-muted-foreground">day
                      <Input type="number" min={1} max={28} className="h-8 w-16 text-xs" value={s.day ?? 1} data-testid={`input-schedule-day-${feed}`}
                        onChange={e => updateSchedule(feed, { ...s, day: Number(e.target.value) })} />
                    </label>
                  )}
                  {(s.frequency === 'daily' || s.frequency === 'monthly') && (
                    <label className="flex items-center gap-1 text-xs text-muted-foreground">hour
                      <Input type="number" min={0} max={23} className="h-8 w-16 text-xs" value={s.hour ?? 0} data-testid={`input-schedule-hour-${feed}`}
                        onChange={e => updateSchedule(feed, { ...s, hour: Number(e.target.value) })} />
                    </label>
                  )}
                  <span className="ml-auto text-[11px] text-muted-foreground">
                    {override ? <button className="underline" onClick={() => updateSchedule(feed, undefined)} data-testid={`button-reset-schedule-${feed}`}>Use default</button> : 'default'}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </section>}

      </fieldset>

      {shownErrors.length > 0 && (
        <ul role="alert" className="list-disc rounded-md border border-destructive/40 bg-destructive/10 py-2 pl-7 pr-3 text-xs text-destructive" data-testid="list-validation-errors">
          {shownErrors.map(e => <li key={e}>{e}</li>)}
        </ul>
      )}
      {save.error && <p role="alert" className="text-xs text-destructive" data-testid="text-save-error">Save failed: {save.error.message}</p>}

      <div className="flex items-center justify-end gap-2 border-t pt-4">
        {dirty && <span className="mr-auto text-[11px] text-warn" data-testid="status-unsaved">Unsaved changes</span>}
        <Button onClick={handleSave} disabled={save.isPending || pending || !dirty} data-testid="button-save-connection">
          {save.isPending ? 'Saving…' : 'Save connection'}
        </Button>
      </div>

      {/* Dry run + pull */}
      <section className="space-y-3 border-t pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="kin-heading mr-auto">Test & pull</h3>
          <select className={`${selectCls} w-44`} value={mappingFilter} data-testid="select-run-mapping" onChange={e => setMappingFilter(e.target.value)}>
            <option value="">All mappings</option>
            {config.mappings.map(m => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select>
          <Button size="sm" variant="outline" disabled={!saved || dirty || dryRun.isPending} data-testid="button-dry-run"
            onClick={() => dryRun.mutate({ mapping_id: mappingFilter || null })}>
            <ScanSearch className="mr-1.5 h-3.5 w-3.5" /> {dryRun.isPending ? 'Checking…' : 'Dry run'}
          </Button>
          <Button size="sm" disabled={!saved || (dirty && !pending) || pull.isPending || !source.is_active} data-testid="button-pull"
            onClick={handlePull}>
            <Play className="mr-1.5 h-3.5 w-3.5" /> {pull.isPending ? 'Queuing…' : 'Pull now'}
          </Button>
        </div>
        {(!saved || dirty) && <p className="text-[11px] text-muted-foreground">Dry run and pull use the saved configuration. Save first.</p>}
        {saved && !source.is_active && <p className="text-[11px] text-muted-foreground">Activate this source to pull.</p>}

        {dryRun.error && <p role="alert" className="text-xs text-destructive" data-testid="text-dry-run-error">{dryRun.error.message}</p>}
        {dryRun.isPending && <div className="h-16 rounded-md bg-muted animate-pulse" />}
        {dryRun.data && (
          <div className="rounded-md border" data-testid="table-dry-run">
            <div className="border-b px-3 py-1.5 text-[11px] text-muted-foreground">Up to {dryRun.data.limit_per_mapping} rows per mapping. Mapped records and identity keys; review before pulling.</div>
            {dryRun.data.items.length === 0 ? (
              <p className="p-3 text-xs text-muted-foreground" data-testid="empty-dry-run">No matching files or rows found.</p>
            ) : dryRun.data.items.map(p => (
              <div key={p.mapping_id} className="border-b last:border-0 px-3 py-2" data-testid={`row-dry-run-${p.mapping_id}`}>
                <div className="flex items-center gap-3 text-xs">
                  <span className="font-mono font-medium">{p.mapping_id}</span>
                  <span>{p.rows_checked.toLocaleString()} checked</span>
                  <span className={p.rows_rejected ? 'text-destructive' : 'text-ok'}>{p.rows_rejected.toLocaleString()} rejected</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {p.fields.map(f => <span key={f} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{f}</span>)}
                </div>
                {p.identity_keys && p.identity_keys.length > 0 && (
                  <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground" data-testid={`identity-keys-${p.mapping_id}`}>
                    identity keys: {p.identity_keys.map(k => <span key={k} className="rounded bg-signal-soft px-1.5 py-0.5 font-mono text-primary">{k}</span>)}
                  </div>
                )}
                {p.suggested_columns && Object.keys(p.suggested_columns).length > 0 && (
                  <Button size="sm" variant="ghost" className="mt-1 h-6 px-2 text-[11px]" data-testid={`button-apply-suggestions-${p.mapping_id}`}
                    onClick={() => setConfig(c => ({ ...c, mappings: c.mappings.map(m => m.id === p.mapping_id ? { ...m, columns: { ...p.suggested_columns, ...(m.columns ?? {}) } } : m) }))}>
                    <Wand2 className="mr-1 h-3 w-3" /> Apply suggested columns
                  </Button>
                )}
                {p.rows && p.rows.length > 0 && (
                  <div className="mt-2 max-h-72 overflow-auto rounded border" data-testid={`rows-dry-run-${p.mapping_id}`}>
                    <table className="w-full text-[10px] font-mono">
                      <thead className="sticky top-0 bg-muted"><tr><th className="px-2 py-1 text-left">#</th>{Object.keys(p.rows[0]).map(h => <th key={h} className="whitespace-nowrap px-2 py-1 text-left">{h}</th>)}</tr></thead>
                      <tbody>
                        {p.rows.slice(0, 100).map((r, ri) => (
                          <tr key={ri} className="border-t"><td className="px-2 py-0.5 text-muted-foreground">{ri + 1}</td>
                            {Object.keys(p.rows![0]).map(h => <td key={h} className="max-w-[200px] truncate whitespace-nowrap px-2 py-0.5" title={String(r[h] ?? '')}>{r[h] === null || r[h] === undefined ? '' : String(r[h])}</td>)}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {pull.data && <p className="text-xs text-ok" data-testid="status-pull-queued">Pull queued as job #{pull.data.job_id}. <Link to="/imports" className="underline" data-testid="link-imports">Follow progress in Imports</Link></p>}
        {pull.error && <p role="alert" className="text-xs text-destructive" data-testid="text-pull-error">{pull.error.message}</p>}
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[11px] text-muted-foreground">
            <span>Pull history</span>
            {pulls.data?.items?.some(i => ACTIVE_PULL_STATUSES.includes(i.status)) && <span data-testid="status-pull-polling">Refreshing every 5s</span>}
          </div>
          {pulls.isLoading ? <div className="h-12 rounded-md bg-muted animate-pulse" data-testid="pulls-loading" /> : pulls.error ? (
            <div role="alert" className="flex items-center gap-2 text-xs text-destructive" data-testid="text-pulls-error">
              Unable to load pull history: {pulls.error.message}
              <Button size="sm" variant="ghost" onClick={() => pulls.refetch()} data-testid="button-retry-pulls">Retry</Button>
            </div>
          ) : <PullHistory items={pulls.data?.items ?? []} />}
        </div>
      </section>
    </div>
  );
}
