import { useState } from 'react';
import { Plus, Pencil, Archive, Send, Lock, ShieldAlert, Trash2, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { toast } from '@/hooks/use-toast';
import { useDestinations, useDestinationActions } from '@/hooks/use-activations';
import type { Destination } from '@/lib/activations';
import { SftpFields } from './sftp-fields';
import { HubspotFields, HubspotDestinationTools } from './hubspot-fields';
import { DEACTIVATE_DEST_COPY, DEST_TYPES, buildDestinationWrite, clearSftpSecrets, destinationFormErrors, destinationToForm, type DestinationForm } from './logic';
import { ConfirmDialog, SkeletonRows, TypeBadge, apiToast, when } from './shared';

export function DestinationsPanel({ isAdmin }: { isAdmin: boolean }) {
  const q = useDestinations();
  const { archive, test } = useDestinationActions();
  const [editing, setEditing] = useState<Destination | null | 'new'>(null);
  const [archiving, setArchiving] = useState<Destination | null>(null);
  const items = q.data?.items ?? [];

  const runTest = (d: Destination) => test.mutate(d.id, d.type === 'sftp' ? {
    onSuccess: () => toast({ title: `Connected to ${d.name}`, description: 'Login, host-key pin and output directory stat succeeded. This does not prove write permission; the first run confirms it.' }),
    onError: e => apiToast(`Connection test for ${d.name} failed`, e),
  } : {
    onSuccess: r => toast({ title: r.delivered ? `Test delivered to ${d.name}` : `Test not delivered to ${d.name}`, description: r.delivered ? 'The endpoint accepted a signed sample payload with no profile data.' : 'The endpoint did not accept the test. Check the URL and headers.', variant: r.delivered ? undefined : 'destructive' }),
    onError: e => apiToast(`Test to ${d.name} failed`, e),
  });

  return <div className="space-y-2">
    <div className="flex items-center justify-between">
      <p className="text-[12px] text-ink-muted">{isAdmin ? 'Admins create and edit destinations. Secrets and headers are write-only.' : 'Read-only. Ask an admin to add or change destinations.'}</p>
      {isAdmin && <Button size="sm" className="h-7" onClick={() => setEditing('new')} data-testid="button-new-destination"><Plus size={13} /> New destination</Button>}
    </div>
    <div className="border border-line rounded-sm bg-surface overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead className="sticky top-0 bg-surface-raised text-ink-muted text-[10px] uppercase tracking-wider"><tr className="h-8 text-left">
          <th className="px-3">Name</th><th className="px-2">Type</th><th className="px-2">Target</th><th className="px-2">Security</th><th className="px-2">State</th><th className="px-2">Created</th><th className="px-2 w-[1%]" />
        </tr></thead>
        <tbody>
          {q.isLoading ? <SkeletonRows cols={7} /> : q.isError ? <tr><td colSpan={7} className="p-6 text-center"><p className="text-danger mb-2">Couldn't load destinations: {(q.error as Error).message}</p><Button size="sm" variant="outline" onClick={() => q.refetch()} data-testid="button-retry-destinations"><RotateCcw size={12} /> Retry</Button></td></tr>
            : !items.length ? <tr><td colSpan={7} className="p-8 text-center text-ink-muted">{isAdmin ? 'No destinations yet. Create one — a CSV export is the simplest start.' : 'No destinations yet. An admin needs to create one before you can activate a segment.'}</td></tr>
            : items.map(d => <tr key={d.id} className="h-8 border-t border-line hover:bg-surface-raised/60" data-testid={`row-destination-${d.id}`}>
              <td className="px-3"><span className="text-ink">{d.name}</span><span className="ml-2 font-mono text-[10px] text-ink-muted">#{d.id}</span></td>
              <td className="px-2"><TypeBadge type={d.type} /></td>
              <td className="px-2 text-ink-muted max-w-[260px] truncate font-mono text-[11px]">{d.type === 'hubspot' ? `${d.config.list_name ?? '—'} · segment #${d.config.segment_id ?? '—'} · ${d.config.schedule?.frequency ?? 'manual'}` : d.type === 'webhook' ? `${d.config.url ?? '—'} · ${d.config.mode ?? 'full'} · ${d.config.batch_size ?? 500}/batch` : d.type === 'sftp' ? `${d.config.output_dir ?? '—'} · ${d.config.export_kind ?? 'audience'}${d.config.gzip ? ' · gzip' : ''} · PII ${d.config.include_pii === false ? 'excluded' : 'included'}` : d.type === 'csv' ? `delimiter "${d.config.delimiter === '\t' ? 'TAB' : d.config.delimiter ?? ','}" · PII ${d.config.include_pii ? 'included' : 'excluded'}` : d.config.meta_audience_id ? `Meta connected · source #${d.config.meta_source_id} · audience ${d.config.meta_audience_id}` : 'hashed file, manual upload'}</td>
              <td className="px-2 text-ink-muted text-[11px]">{d.type === 'hubspot' ? <span className="inline-flex items-center gap-1" data-testid={`text-credentials-${d.id}`}>{d.has_credentials ? <><Lock size={11} className="text-ok" /> token on file</> : <span className="text-warn">no token</span>}</span> : d.type === 'sftp' ? <span className="inline-flex items-center gap-1" data-testid={`text-credentials-${d.id}`}>{d.has_credentials ? <><Lock size={11} className="text-ok" /> key on file</> : <span className="text-warn">no credentials</span>}{d.config.export_kind === 'crosswalk' && <span className={d.config.approved_purpose ? 'text-ink-muted' : 'text-warn'}> · {d.config.approved_purpose ? 'purpose approved' : 'purpose missing'}</span>}</span> : d.type === 'webhook' ? <span className="inline-flex items-center gap-1">{d.has_secret ? <><Lock size={11} className="text-ok" /> secret ••••{d.secret_last4}</> : <span className="text-warn">unsigned</span>}{d.header_names.length > 0 && <span className="font-mono"> · {d.header_names.join(', ')}</span>}{d.config.allow_internal_network && <span className="text-warn inline-flex items-center gap-0.5"> · <ShieldAlert size={11} /> internal</span>}</span> : '—'}</td>
              <td className="px-2">{d.is_active ? <span className="text-ok">active</span> : <span className="text-ink-muted">inactive</span>}</td>
              <td className="px-2 text-ink-muted font-mono text-[11px]">{when(d.created_at)}</td>
              <td className="px-2 whitespace-nowrap">{isAdmin && <div className="flex gap-1">
                {d.type === 'hubspot' && <HubspotDestinationTools d={d} />}
                {(d.type === 'webhook' || (d.type === 'sftp' && d.has_credentials)) && <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" disabled={test.isPending} onClick={() => runTest(d)} title={d.type === 'sftp' ? 'Connection-only check: login, host key and output directory. Does not test write permission.' : undefined} data-testid={`button-test-destination-${d.id}`}><Send size={11} /> {d.type === 'sftp' ? 'Test connection' : 'Test'}</Button>}
                <Button size="sm" variant="ghost" className="h-6 px-2" onClick={() => setEditing(d)} aria-label={`Edit ${d.name}`} data-testid={`button-edit-destination-${d.id}`}><Pencil size={11} /></Button>
                <Button size="sm" variant="ghost" className="h-6 px-2 text-danger" onClick={() => setArchiving(d)} aria-label={`Deactivate ${d.name}`} title="Deactivate" data-testid={`button-deactivate-destination-${d.id}`}><Archive size={11} /></Button>
              </div>}</td>
            </tr>)}
        </tbody>
      </table>
    </div>
    {editing && <DestinationDialog destination={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    <ConfirmDialog open={!!archiving} onOpenChange={o => !o && setArchiving(null)} testId="deactivate-destination" danger confirm={DEACTIVATE_DEST_COPY.button}
      title={DEACTIVATE_DEST_COPY.title(archiving?.name ?? '')} body={DEACTIVATE_DEST_COPY.body}
      onConfirm={() => archiving && archive.mutate(archiving.id, { onSuccess: () => toast({ title: DEACTIVATE_DEST_COPY.done(archiving.name) }), onError: e => apiToast('Deactivate failed', e) })} />
  </div>;
}

function DestinationDialog({ destination, onClose }: { destination: Destination | null; onClose: () => void }) {
  const { save } = useDestinationActions();
  // Initialized once on open; 10s destination polling never resets what the admin is typing.
  const [f, setF] = useState<DestinationForm>(() => destinationToForm(destination));
  const [touched, setTouched] = useState(false);
  const errors = destinationFormErrors(f);
  const set = <K extends keyof DestinationForm>(k: K, v: DestinationForm[K]) => setF(p => ({ ...p, [k]: v }));
  const submit = () => {
    setTouched(true);
    if (Object.keys(errors).length) return;
    const body = buildDestinationWrite(f);
    save.mutate(body, {
      // Secrets live only in this dialog's state: wipe them as soon as the request settles.
      onSuccess: d => { setF(p => ({ ...p, secret: '', sftp: clearSftpSecrets(p.sftp), hubspot: { ...p.hubspot, token: '' } })); toast({ title: `${destination ? 'Saved' : 'Created'} ${d.name}` }); onClose(); },
      onError: e => apiToast('Save failed', e),
    });
  };
  const err = (k: string) => touched && errors[k] ? <p className="text-danger text-[11px] mt-0.5" data-testid={`error-${k}`}>{errors[k]}</p> : null;

  return <Dialog open onOpenChange={o => { if (!o) { setF(p => ({ ...p, secret: '', sftp: clearSftpSecrets(p.sftp), hubspot: { ...p.hubspot, token: '' } })); onClose(); } }}>
    <DialogContent className="bg-surface border-line max-w-xl max-h-[90dvh] overflow-y-auto">
      <DialogHeader><DialogTitle className="kin-heading">{destination ? `Edit ${destination.name}` : 'New destination'}</DialogTitle>
        <DialogDescription className="text-ink-muted text-[12px]">Nothing is sent while you edit. Webhook tests run only when you click Test.</DialogDescription></DialogHeader>
      <div className="space-y-3 text-[13px]">
        <div className="grid grid-cols-2 gap-3">
          <div><Label htmlFor="d-name" className="text-[11px] text-ink-muted">Name</Label><Input id="d-name" className="h-8" value={f.name} onChange={e => set('name', e.target.value)} data-testid="input-destination-name" />{err('name')}</div>
          <div><Label className="text-[11px] text-ink-muted">Active</Label><div className="h-8 flex items-center gap-2"><Switch checked={f.is_active} onCheckedChange={v => set('is_active', v)} data-testid="switch-destination-active" /><span className="text-ink-muted text-[12px]">{f.is_active ? 'Available to activations' : 'Hidden from new activations'}</span></div></div>
        </div>
        <fieldset disabled={!!destination}>
          <legend className="text-[11px] text-ink-muted mb-1">Type {destination && '(fixed after creation)'}</legend>
          <div className="grid grid-cols-2 gap-1.5">{DEST_TYPES.map(t => <label key={t.value} className={`border rounded-sm p-2 cursor-pointer ${f.type === t.value ? 'border-signal bg-signal-soft' : 'border-line hover:border-line-strong'}`}>
            <input type="radio" name="dtype" className="sr-only" checked={f.type === t.value} onChange={() => set('type', t.value)} data-testid={`radio-type-${t.value}`} />
            <div className="flex items-center gap-1.5"><TypeBadge type={t.value} /></div><p className="text-[11px] text-ink-muted mt-1 leading-snug">{t.blurb}</p></label>)}</div>
        </fieldset>
        {f.meta && <p className="text-[12px] text-ink-muted border border-signal/40 rounded-sm p-2 bg-signal-soft" data-testid="info-meta-connected"><strong className="text-ink">Connected mode.</strong> This destination is linked to a Kinship-owned Meta custom audience (source #{f.meta.meta_source_id}, audience <span className="font-mono">{f.meta.meta_audience_id}</span>). These links are fixed; it is not a manual file. Viewing / VOD rules and attributes are blocked for every Meta export. New connected audiences are created from the segment's Meta results tab.</p>}
        {!f.meta && (f.type === 'google_customer_match' || f.type === 'meta_custom_audience') && <p className="text-[12px] text-ink-muted border border-line rounded-sm p-2 bg-surface-raised">Produces a normalized, SHA-256 hashed CSV for <strong className="text-ink">manual upload</strong>. No API key or account connection is used or stored; nothing is pushed to the ad platform.</p>}
        {f.type === 'csv' && <div className="grid grid-cols-2 gap-3">
          <div><Label className="text-[11px] text-ink-muted">Delimiter</Label>
            <select className="h-8 w-full bg-ground border border-line-strong rounded-sm px-2" value={f.delimiter} onChange={e => set('delimiter', e.target.value)} data-testid="select-delimiter">
              <option value=",">Comma ,</option><option value=";">Semicolon ;</option><option value="|">Pipe |</option><option value={'\t'}>Tab</option></select>{err('delimiter')}</div>
          <div><Label className="text-[11px] text-ink-muted">PII columns</Label><div className="h-8 flex items-center gap-2"><Switch checked={f.include_pii} onCheckedChange={v => set('include_pii', v)} data-testid="switch-include-pii" /><span className="text-[12px] text-ink-muted">{f.include_pii ? 'Email, phone, names allowed' : 'PII fields blocked from mapping'}</span></div></div>
        </div>}
        {f.type === 'hubspot' && <HubspotFields value={f.hubspot} onChange={v => set('hubspot', v)} hasToken={!!destination?.has_credentials} err={err} />}
        {f.type === 'sftp' && <SftpFields value={f.sftp} includePii={f.include_pii} onIncludePii={v => set('include_pii', v)} onChange={v => set('sftp', v)} hasCredentials={!!destination?.has_credentials} editing={!!destination} errors={touched ? errors : {}} />}
        {f.type === 'webhook' && <>
          <div><Label htmlFor="d-url" className="text-[11px] text-ink-muted">URL (POST)</Label><Input id="d-url" className="h-8 font-mono text-[12px]" placeholder="https://partner.example.org/audiences" value={f.url} onChange={e => set('url', e.target.value)} data-testid="input-webhook-url" />{err('url')}</div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label htmlFor="d-batch" className="text-[11px] text-ink-muted">Batch size</Label><Input id="d-batch" inputMode="numeric" className="h-8 font-mono" value={f.batch_size} onChange={e => set('batch_size', e.target.value)} data-testid="input-batch-size" />{err('batch_size')}</div>
            <div><Label className="text-[11px] text-ink-muted">Mode</Label>
              <select className="h-8 w-full bg-ground border border-line-strong rounded-sm px-2" value={f.mode} onChange={e => set('mode', e.target.value as 'full' | 'delta')} data-testid="select-mode"><option value="full">full — all current members</option><option value="delta">delta — added/removed since last success</option></select></div>
          </div>
          <div><Label htmlFor="d-secret" className="text-[11px] text-ink-muted">Signing secret (HMAC-SHA256)</Label>
            <Input id="d-secret" type="password" autoComplete="new-password" className="h-8 font-mono" placeholder={destination?.has_secret ? `Leave blank to keep ••••${destination.secret_last4 ?? ''}` : 'Enter a secret'} value={f.secret} onChange={e => set('secret', e.target.value)} data-testid="input-signing-secret" />
            <p className="text-[11px] text-ink-muted mt-0.5">Write-only, encrypted at rest. Never shown again.</p></div>
          <div className="border border-line rounded-sm p-2">
            <div className="flex items-center justify-between"><span className="text-[11px] text-ink-muted">Custom headers {destination && destination.header_names.length > 0 && <span className="font-mono">(current: {destination.header_names.join(', ')})</span>}</span>
              {destination && <label className="flex items-center gap-1.5 text-[11px]"><Checkbox checked={f.replaceHeaders} onCheckedChange={v => set('replaceHeaders', !!v)} data-testid="checkbox-replace-headers" /> Replace headers</label>}</div>
            {f.replaceHeaders ? <div className="space-y-1 mt-1.5">
              {f.headers.map((h, i) => <div key={i} className="flex gap-1">
                <Input className="h-7 font-mono text-[11px]" placeholder="X-Partner-Key" value={h.name} onChange={e => set('headers', f.headers.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} data-testid={`input-header-name-${i}`} />
                <Input type="password" autoComplete="off" className="h-7 font-mono text-[11px]" placeholder="value (write-only)" value={h.value} onChange={e => set('headers', f.headers.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} data-testid={`input-header-value-${i}`} />
                <Button size="sm" variant="ghost" className="h-7 px-2" aria-label="Remove header" onClick={() => set('headers', f.headers.filter((_, j) => j !== i))} data-testid={`button-remove-header-${i}`}><Trash2 size={11} /></Button></div>)}
              <Button size="sm" variant="outline" className="h-6 text-[11px]" onClick={() => set('headers', [...f.headers, { name: '', value: '' }])} data-testid="button-add-header"><Plus size={11} /> Header</Button>
              {destination && <p className="text-[11px] text-warn">Saving replaces all existing headers with exactly this list.</p>}{err('headers')}
            </div> : <p className="text-[11px] text-ink-muted mt-1">Existing header values are preserved.</p>}
          </div>
          <label className="flex items-start gap-2 border border-warn/40 bg-warn/5 rounded-sm p-2">
            <Checkbox checked={f.allow_internal_network} onCheckedChange={v => set('allow_internal_network', !!v)} className="mt-0.5" data-testid="checkbox-allow-internal" />
            <span className="text-[12px]"><span className="text-ink font-medium inline-flex items-center gap-1"><ShieldAlert size={12} className="text-warn" /> Allow internal network</span><br />
              <span className="text-ink-muted">Off by default. Turning it on lets this webhook reach private, loopback and link-local addresses (10.x, 127.0.0.1, 169.254.169.254) — a server-side request forgery risk. Only enable for a vetted on-prem receiver. The change is recorded in the audit log.</span></span>
          </label>
        </>}
      </div>
      <DialogFooter>
        <Button variant="outline" size="sm" onClick={onClose} data-testid="button-cancel-destination">Cancel</Button>
        <Button size="sm" onClick={submit} disabled={save.isPending} data-testid="button-save-destination">{save.isPending ? 'Saving…' : destination ? 'Save changes' : 'Create destination'}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
