import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from '@/hooks/use-toast';
import type { Source } from '@/hooks/use-sources';
import { useConnectMeta, useMetaConnection, useMetaForms, useMetaRuns, useRunMeta, useSaveMetaConnection, type MetaConfig } from '@/hooks/use-meta';

const EMPTY: MetaConfig = { page_id: '', app_id: '', graph_version: 'v25.0', backfill_days: 30, field_mapping: {}, consent_form_id: '', consent_field: '', consent_text: '', consent_channel: 'email', consent_true_values: [], ad_account_id: '' };
const SECRETS = ['page_access_token', 'app_secret', 'verify_token', 'ads_access_token'] as const;

export function MetaSourcePanel({ source }: { source: Source }) {
  const q = useMetaConnection(source.id);
  const save = useSaveMetaConnection(source.id);
  const connect = useConnectMeta(source.id);
  const run = useRunMeta(source.id);
  const [cfg, setCfg] = useState<MetaConfig>(EMPTY);
  const [mapText, setMapText] = useState('');
  const [trueText, setTrueText] = useState('');
  const [creds, setCreds] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);
  const runs = useMetaRuns(source.id);
  const forms = useMetaForms(source.id, !!q.data?.configured && !!q.data?.has_credentials);

  useEffect(() => {
    if (q.data && !loaded) {
      const c = { ...EMPTY, ...q.data.config };
      setCfg(c);
      setMapText(Object.entries(c.field_mapping ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'));
      setTrueText((c.consent_true_values ?? []).join(', '));
      setLoaded(true);
    }
  }, [q.data, loaded]);

  const set = <K extends keyof MetaConfig>(k: K, v: MetaConfig[K]) => setCfg(c => ({ ...c, [k]: v }));
  const err = (t: string) => (e: any) => toast({ title: t, description: e.message, variant: 'destructive' });

  const onSave = () => {
    const field_mapping: Record<string, string> = {};
    for (const line of mapText.split('\n')) { const [k, ...r] = line.split('='); if (k?.trim() && r.join('=').trim()) field_mapping[k.trim()] = r.join('=').trim(); }
    const config = { ...cfg, backfill_days: Number(cfg.backfill_days) || 0, field_mapping, consent_true_values: trueText.split(',').map(s => s.trim()).filter(Boolean) };
    const credentials: Record<string, string> = {};
    for (const k of SECRETS) if (creds[k]?.trim()) credentials[k] = creds[k].trim();
    save.mutate({ config, ...(Object.keys(credentials).length ? { credentials } : {}) }, {
      onSuccess: () => { setCreds({}); toast({ title: 'Meta connection saved' }); },
      onError: err('Error saving connection'),
    });
  };

  if (q.isLoading) return <Skeleton className="h-64" />;
  if (q.isError) return <div role="alert" className="text-sm text-destructive">Unable to load connection: {(q.error as Error).message} <Button size="sm" variant="outline" onClick={() => q.refetch()}>Retry</Button></div>;
  const ready = !!q.data?.configured && !!q.data?.has_credentials;
  const field = (label: string, k: keyof MetaConfig, ph = '') => (
    <div className="space-y-1"><Label htmlFor={`meta-${k}`}>{label}</Label>
      <Input id={`meta-${k}`} data-testid={`input-meta-${k}`} value={String(cfg[k] ?? '')} placeholder={ph} onChange={e => set(k, e.target.value as never)} /></div>
  );

  return (
    <div className="space-y-5" data-testid="panel-meta-source">
      <p className="text-xs text-muted-foreground rounded-md border p-3">
        Tokens are write-only. Saved secrets are never shown again; leave a field blank to keep the stored value. Meta sends lead notifications to the callback path below, which uses your verify token.
      </p>
      {q.data?.webhook_path && <div className="space-y-1"><Label>Saved callback path</Label><code data-testid="text-meta-webhook" className="block rounded-md border bg-muted px-2 py-1.5 text-xs break-all">{q.data.webhook_path}</code></div>}
      <div className="grid sm:grid-cols-2 gap-3">
        {field('Page ID', 'page_id')}{field('App ID', 'app_id')}
        {field('Graph version', 'graph_version', 'v25.0')}{field('Backfill days', 'backfill_days')}
        {field('Ad account ID', 'ad_account_id', 'act_1234567890')}
      </div>
      <p className="text-xs text-muted-foreground">An ads access token and ad account ID are needed only for custom audiences and campaign results.</p>
      <div className="space-y-1"><Label htmlFor="meta-map">Field mapping (meta_field=canonical_field, one per line)</Label>
        <textarea id="meta-map" data-testid="input-meta-mapping" value={mapText} onChange={e => setMapText(e.target.value)} className="w-full h-24 rounded-md border border-input bg-background p-2 font-mono text-xs" placeholder={'email=email\nfull_name=first_name'} /></div>
      <div className="grid sm:grid-cols-2 gap-3">
        {field('Consent form ID', 'consent_form_id')}
        {field('Consent field', 'consent_field')}{field('Consent channel', 'consent_channel', 'email')}
        {field('Consent text', 'consent_text')}
        <div className="space-y-1"><Label htmlFor="meta-true">Consent true values (comma-separated)</Label><Input id="meta-true" data-testid="input-meta-true-values" value={trueText} onChange={e => setTrueText(e.target.value)} placeholder="yes, true, 1" /></div>
      </div>
      <p className="text-xs text-muted-foreground">Consent is bound to the exact form ID, checkbox field and wording. Missing or changed wording never grants permission. After changing mappings or consent settings, save and use Run now to reprocess recent leads.</p>
      <div className="grid sm:grid-cols-2 gap-3">
        {SECRETS.map(k => (
          <div key={k} className="space-y-1"><Label htmlFor={`meta-cred-${k}`}>{k.replace(/_/g, ' ')}{k === 'ads_access_token' ? ' (optional)' : ''}</Label>
            <Input id={`meta-cred-${k}`} type="password" autoComplete="off" data-testid={`input-meta-${k}`} value={creds[k] ?? ''} onChange={e => setCreds(c => ({ ...c, [k]: e.target.value }))} placeholder={q.data?.has_credentials ? 'Stored - leave blank to keep' : ''} /></div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button onClick={onSave} disabled={save.isPending} data-testid="button-meta-save">{save.isPending ? 'Saving...' : 'Save connection'}</Button>
        <Button variant="outline" disabled={!ready || connect.isPending} data-testid="button-meta-connect"
          onClick={() => connect.mutate(undefined, { onSuccess: r => toast({ title: `Connected ${r.page.name}`, description: `Page subscribed; recent-lead backfill started (job #${r.job_id}).` }), onError: err('Connect failed') })}>
          {connect.isPending ? 'Connecting...' : 'Connect and backfill'}</Button>
        <Button variant="outline" disabled={!ready || run.isPending} data-testid="button-meta-run"
          onClick={() => run.mutate(undefined, { onSuccess: r => toast({ title: 'Lead pull queued', description: `Job #${r.job_id}` }), onError: err('Run failed') })}>Run now</Button>
      </div>
      <p className="text-xs text-muted-foreground">Connect subscribes the page to lead notifications and starts a backfill of recent leads. Progress appears in job history.</p>
      <div className="space-y-1" data-testid="panel-meta-runs"><Label>Run and backfill history</Label>
        {runs.isLoading ? <Skeleton className="h-16" /> : runs.isError ? <p className="text-xs text-destructive">{(runs.error as Error).message} <button className="underline" onClick={() => runs.refetch()}>Retry</button></p> : <>
          <div className="flex gap-4 text-xs font-mono" data-testid="text-meta-leads"><span>queued {runs.data!.leads.queued}</span><span>completed {runs.data!.leads.completed}</span><span>suppressed {runs.data!.leads.suppressed}</span></div>
          {!runs.data!.items.length ? <p className="text-xs text-muted-foreground">No runs yet.</p> : <ul className="text-xs rounded-md border divide-y">{runs.data!.items.slice(0, 8).map(j => <li key={j.id} className="flex flex-wrap gap-2 px-2 py-1.5"><span className="font-mono">job #{j.id}</span><span className="font-mono uppercase">{j.status}</span><span className="text-muted-foreground">{new Date(j.created_at).toLocaleString()}</span>{j.error && <span className="text-destructive">{j.error}</span>}</li>)}</ul>}</>}</div>
      {ready && <div className="space-y-1"><Label>Lead forms</Label>
        {forms.isLoading ? <Skeleton className="h-8" /> : forms.isError ? <p className="text-xs text-destructive">{(forms.error as Error).message} <button className="underline" onClick={() => forms.refetch()}>Retry</button></p>
          : !forms.data?.items.length ? <p className="text-xs text-muted-foreground">No lead forms found for this page.</p>
          : <ul className="text-xs rounded-md border divide-y">{forms.data.items.map(f => <li key={f.id} className="flex justify-between px-2 py-1.5"><span>{f.name} <span className="font-mono text-muted-foreground">{f.id}</span></span><span className="font-mono uppercase">{f.status}</span></li>)}</ul>}</div>}
    </div>
  );
}
