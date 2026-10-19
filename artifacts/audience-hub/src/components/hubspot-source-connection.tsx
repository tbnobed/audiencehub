import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, KeyRound, Play, Plug, RefreshCw, ScanSearch, Wand2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import type { Source } from '@/hooks/use-sources';
import { useSourcePull, useSourcePulls, ACTIVE_PULL_STATUSES } from '@/hooks/use-source-connection';
import { useHsConnection, useHsSave, useHsSourceTest, useHsProperties, useHsDryRun, type HsConfig, type HsMapping, type HsProperty } from '@/hooks/use-hubspot';
import { ScheduleEditor, TestResult } from '@/components/hubspot-shared';

const blank = (): HsMapping => ({ id: 'contacts', feed: 'people', properties: [], columns: {}, schedule: { frequency: 'manual', timezone: 'America/New_York' } });
const clean = (m: HsMapping, engagement: boolean): HsConfig => {
  const s = m.schedule; const sc: HsMapping['schedule'] = { frequency: s.frequency, timezone: 'America/New_York' };
  if (s.frequency !== 'manual') sc.minute = s.minute ?? 0;
  if (s.frequency === 'daily' || s.frequency === 'monthly') sc.hour = s.hour ?? 0;
  if (s.frequency === 'monthly') sc.day = s.day ?? 1;
  const columns = Object.fromEntries(Object.entries(m.columns).filter(([k, v]) => m.properties.includes(k) && v.trim()).map(([k, v]) => [k, v.trim()]));
  return { mappings: [{ id: 'contacts', feed: 'people', properties: m.properties, columns, schedule: sc }], schedules: {}, engagement };
};
const fmt = (v: string | null) => (v ? new Date(v).toLocaleString() : '—');

export function HubspotSourceConnection({ source }: { source: Source }) {
  const conn = useHsConnection(source.id);
  const save = useHsSave(source.id);
  const test = useHsSourceTest(source.id);
  const props = useHsProperties(source.id);
  const dry = useHsDryRun(source.id);
  const pull = useSourcePull(source.id);
  const pulls = useSourcePulls(source.id);
  const [m, setM] = useState<HsMapping>(blank());
  const [engagement, setEngagement] = useState(false);
  const [available, setAvailable] = useState<HsProperty[]>([]);
  const [filter, setFilter] = useState('');
  const [token, setToken] = useState('');
  const [replace, setReplace] = useState(false);
  const [savedJson, setSavedJson] = useState('');
  const initFor = useRef<number | null>(null);

  useEffect(() => {
    if (conn.data && initFor.current !== source.id) {
      initFor.current = source.id;
      const cm = conn.data.config.mappings?.[0];
      const mm = cm ? { ...blank(), ...cm, columns: cm.columns ?? {}, properties: cm.properties ?? [] } : blank();
      setM(mm); setEngagement(!!conn.data.config.engagement); setReplace(!conn.data.has_credentials);
      setSavedJson(JSON.stringify(clean(mm, !!conn.data.config.engagement)));
    }
  }, [conn.data, source.id]);
  useEffect(() => () => setToken(''), []);

  const cfg = clean(m, engagement);
  const dirty = JSON.stringify(cfg) !== savedJson || (replace && !!token);
  const needToken = replace && !token.trim();
  const tokenArg = replace && token.trim() ? { access_token: token.trim() } : undefined;
  const saved = !!conn.data?.has_credentials;

  const loadProps = () => props.mutate(undefined, { onSuccess: d => {
    setAvailable(d.items);
    setM(p => ({ ...p, columns: { ...d.suggested_columns, ...Object.fromEntries(Object.entries(p.columns).filter(([, v]) => v)) } }));
  } });
  const toggle = (name: string, on: boolean) => setM(p => ({ ...p, properties: on ? [...p.properties, name] : p.properties.filter(x => x !== name) }));
  const doSave = () => {
    if (needToken) return;
    save.mutate({ config: cfg, credentials: tokenArg }, { onSuccess: () => { setToken(''); setReplace(false); setSavedJson(JSON.stringify(cfg)); dry.reset(); } });
  };

  if (conn.isLoading) return <div className="space-y-3" role="status" data-testid="connection-loading">{[0, 1, 2].map(i => <div key={i} className="h-10 rounded-md bg-muted animate-pulse" />)}</div>;
  if (conn.error) return <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm" data-testid="connection-error">
    <p className="font-medium text-destructive">Unable to load connection.</p><p className="text-muted-foreground mt-1">{conn.error.message}</p>
    <Button size="sm" variant="outline" className="mt-3" onClick={() => conn.refetch()}><RefreshCw className="mr-2 h-3.5 w-3.5" /> Retry</Button></div>;

  const names = Array.from(new Set([...available.map(p => p.name), ...m.properties]));
  const shown = names.filter(n => !filter || n.toLowerCase().includes(filter.toLowerCase()));
  const label = (n: string) => available.find(p => p.name === n)?.label ?? n;

  return <div className="space-y-6 text-sm" data-testid="hubspot-source-connection">
    <section className="space-y-3">
      <h3 className="kin-heading flex items-center gap-2"><KeyRound className="h-4 w-4 text-primary" /> HubSpot access token</h3>
      {saved && !replace ? <div className="flex items-center justify-between rounded-md border bg-muted/40 px-3 py-2" data-testid="status-credentials-saved">
        <span className="flex items-center gap-2 text-xs"><CheckCircle2 className="h-4 w-4 text-ok" /> Encrypted token saved</span>
        <Button size="sm" variant="outline" onClick={() => setReplace(true)} data-testid="button-replace-credentials">Replace</Button></div>
        : <div className="space-y-2 rounded-md border p-3">
          <p className="text-xs text-muted-foreground">Private app token. Write-only: sent once, encrypted at rest, never shown again.</p>
          <Label htmlFor="hs-token" className="text-xs">Access token <span className="text-destructive">*</span></Label>
          <Input id="hs-token" type="password" autoComplete="new-password" className="h-8 font-mono text-xs" value={token} onChange={e => setToken(e.target.value)} data-testid="input-hubspot-source-token" />
          {saved && <Button size="sm" variant="ghost" onClick={() => { setReplace(false); setToken(''); }}>Keep saved token</Button>}
        </div>}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={test.isPending || (needToken && !saved)} onClick={() => test.mutate({ credentials: tokenArg })} data-testid="button-test-connection"><Plug className="mr-1.5 h-3.5 w-3.5" /> {test.isPending ? 'Testing…' : saved && !replace ? 'Test saved token' : 'Test token'}</Button>
      </div>
      {test.error && <p role="alert" className="text-xs text-destructive" data-testid="text-test-error">Test failed: {test.error.message}</p>}
      {test.data && <TestResult r={test.data} testId="result-source-test" />}
    </section>

    <section className="space-y-3">
      <div className="flex items-center gap-2"><h3 className="kin-heading mr-auto">Contact properties</h3>
        <Button size="sm" variant="outline" disabled={props.isPending || !saved} onClick={loadProps} data-testid="button-load-properties"><Wand2 className="mr-1 h-3.5 w-3.5" /> {props.isPending ? 'Loading…' : 'Load from HubSpot'}</Button></div>
      {!saved && <p className="text-[11px] text-muted-foreground">Save the token first to load properties.</p>}
      {props.error && <p role="alert" className="text-xs text-destructive">{props.error.message}</p>}
      {names.length === 0 ? <div className="rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground" data-testid="empty-properties">No properties selected. Load properties to choose which contact fields to ingest.</div> : <>
        <Input className="h-8 text-xs" placeholder="Filter properties" value={filter} onChange={e => setFilter(e.target.value)} data-testid="input-filter-properties" />
        <div className="max-h-72 space-y-1 overflow-auto">
          {shown.map(n => { const on = m.properties.includes(n); return <div key={n} className="flex items-center gap-2 rounded border bg-background p-1.5">
            <input type="checkbox" checked={on} onChange={e => toggle(n, e.target.checked)} aria-label={`Select ${n}`} data-testid={`checkbox-property-${n}`} />
            <span className="w-1/3 truncate text-xs" title={n}>{label(n)} <span className="font-mono text-[10px] text-muted-foreground">{n}</span></span>
            <span className="text-muted-foreground">→</span>
            <Input className="h-7 flex-1 font-mono text-[11px]" disabled={!on} placeholder="canonical field (blank keeps as extra attribute)" value={m.columns[n] ?? ''} data-testid={`input-column-${n}`}
              onChange={e => setM(p => ({ ...p, columns: { ...p.columns, [n]: e.target.value } }))} /></div>; })}
        </div></>}
    </section>

    <section className="space-y-3">
      <div className="flex items-center gap-2"><Switch id="hs-eng" checked={engagement} onCheckedChange={setEngagement} data-testid="switch-engagement" /><Label htmlFor="hs-eng" className="text-xs">Ingest email engagement (opens, clicks, bounces)</Label></div>
      <ScheduleEditor testId="hubspot-source" value={m.schedule} onChange={s => setM(p => ({ ...p, schedule: s }))} />
    </section>

    <div className="flex flex-wrap items-center gap-2 border-t pt-4">
      <Button size="sm" onClick={doSave} disabled={save.isPending || !dirty || needToken} data-testid="button-save-connection">{save.isPending ? 'Saving…' : 'Save connection'}</Button>
      <Button size="sm" variant="outline" disabled={dirty || !saved || dry.isPending} onClick={() => dry.mutate()} data-testid="button-dry-run"><ScanSearch className="mr-1.5 h-3.5 w-3.5" /> {dry.isPending ? 'Checking…' : 'Preview 100 rows'}</Button>
      <Button size="sm" variant="outline" disabled={dirty || !saved || pull.isPending} onClick={() => pull.mutate({})} data-testid="button-pull-now"><Play className="mr-1.5 h-3.5 w-3.5" /> {pull.isPending ? 'Queuing…' : 'Pull now'}</Button>
      {dirty && <span className="text-[11px] text-muted-foreground">Save to enable preview and pull.</span>}
    </div>
    {save.error && <p role="alert" className="text-xs text-destructive" data-testid="text-save-error">{save.error.message}</p>}
    {pull.error && <p role="alert" className="text-xs text-destructive">{pull.error.message}</p>}
    {pull.data && <p className="text-xs text-ok" data-testid="status-pull-queued">Pull queued as job #{pull.data.job_id}.</p>}
    {dry.error && <p role="alert" className="text-xs text-destructive" data-testid="text-dry-error">{dry.error.message}</p>}
    {dry.data?.items.map(it => <div key={it.mapping_id} className="space-y-2 rounded-md border p-3" data-testid={`dry-run-${it.mapping_id}`}>
      <p className="text-xs">Mapping <span className="font-mono">{it.mapping_id}</span>: checked {it.rows_checked.toLocaleString()} (limit {dry.data.limit_per_mapping}), rejected <span className={it.rows_rejected ? 'text-destructive' : ''}>{it.rows_rejected.toLocaleString()}</span>{it.identity_keys?.length ? <> · identity keys <span className="font-mono">{it.identity_keys.join(', ')}</span></> : null}</p>
      {it.suggested_columns && Object.keys(it.suggested_columns).length > 0 && <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={() => setM(p => ({ ...p, columns: { ...it.suggested_columns, ...Object.fromEntries(Object.entries(p.columns).filter(([, v]) => v)) } }))}>Apply suggested columns</Button>}
      {it.rows && it.rows.length > 0 ? <div className="max-h-72 overflow-auto"><table className="w-full text-[11px] font-mono"><thead className="text-left text-muted-foreground"><tr>{it.fields.map(f => <th key={f} className="pr-3 whitespace-nowrap">{f}</th>)}</tr></thead>
        <tbody>{it.rows.map((r, i) => <tr key={i} className="border-t">{it.fields.map(f => <td key={f} className="pr-3 whitespace-nowrap">{r[f] === null || r[f] === undefined ? '' : typeof r[f] === 'object' ? JSON.stringify(r[f]) : String(r[f])}</td>)}</tr>)}</tbody></table></div>
        : <p className="text-xs text-muted-foreground">No rows returned.</p>}
    </div>)}

    <section className="space-y-2">
      <h3 className="kin-heading">Pull history</h3>
      {pulls.isError ? <p role="alert" className="text-xs text-destructive">{(pulls.error as Error).message}</p>
        : !pulls.data?.items.length ? <p className="rounded-md border border-dashed p-3 text-center text-xs text-muted-foreground" data-testid="empty-pulls">No pulls yet.</p>
        : <div className="divide-y rounded-md border" data-testid="list-pulls">{pulls.data.items.map(p => <div key={p.job_id} className="space-y-1 px-3 py-2 text-xs" data-testid={`row-pull-${p.job_id}`}>
          <div className="flex flex-wrap items-center gap-3"><span className="font-mono">job #{p.job_id}</span>
            <span className={`rounded px-1.5 text-[10px] uppercase ${p.status === 'completed' ? 'bg-ok/15 text-ok' : p.status === 'failed' || p.status === 'dead' ? 'bg-destructive/15 text-destructive' : ACTIVE_PULL_STATUSES.includes(p.status) ? 'bg-signal-soft text-primary' : 'bg-muted text-muted-foreground'}`}>{p.status}</span>
            <span className="ml-auto text-muted-foreground">{fmt(p.created_at)}</span></div>
          <div className="flex flex-wrap gap-x-4 text-[11px] text-muted-foreground"><span>rows {p.counts.rows_total.toLocaleString()}</span><span className="text-ok">ok {p.counts.rows_ok.toLocaleString()}</span><span className={p.counts.rows_rejected ? 'text-destructive' : ''}>rejected {p.counts.rows_rejected.toLocaleString()}</span></div>
          {p.error && <p role="alert" className="text-[11px] text-destructive">{p.error}</p>}</div>)}</div>}
    </section>
  </div>;
}
