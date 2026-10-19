import { useState, type ReactNode } from 'react';
import { Eye, Play, Plug } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { toast } from '@/hooks/use-toast';
import { useSegments } from '@/hooks/use-segments';
import { useHsDestinationPreview, useHsDestinationRun, useHsDestinationTest, type HsTest } from '@/hooks/use-hubspot';
import { ScheduleEditor, TestResult } from '@/components/hubspot-shared';
import type { Destination } from '@/lib/activations';
import type { HubspotForm } from './logic';
import { apiToast } from './shared';

export function HubspotFields({ value, onChange, hasToken, err }: { value: HubspotForm; onChange: (v: HubspotForm) => void; hasToken: boolean; err: (k: string) => ReactNode }) {
  const segs = useSegments();
  const set = (p: Partial<HubspotForm>) => onChange({ ...value, ...p });
  const items = (segs.data?.items ?? []).filter(s => s.status !== 'archived');
  return <div className="space-y-3" data-testid="fields-hubspot">
    <div className="text-[12px] text-ink-muted border border-line rounded-sm p-2 bg-surface-raised space-y-1" data-testid="notice-hubspot-list">
      <p><span className="font-medium text-ink">Use a dedicated static list.</span> Each sync removes members who are outside the consented segment, so any manually added memberships will be removed.</p>
      <p>Master IDs are written to HubSpot contacts for everyone in the selected segment, but email consent alone controls list membership. The access token is write-only.</p>
    </div>
    <div className="grid grid-cols-2 gap-3">
      <div><Label className="text-[11px] text-ink-muted">HubSpot list name</Label><Input className="h-8" value={value.list_name} onChange={e => set({ list_name: e.target.value })} data-testid="input-hubspot-list-name" />{err('list_name')}</div>
      <div><Label className="text-[11px] text-ink-muted">Existing list ID (optional)</Label><Input className="h-8 font-mono" inputMode="numeric" value={value.list_id} onChange={e => set({ list_id: e.target.value })} data-testid="input-hubspot-list-id" />{err('list_id')}</div>
    </div>
    <div><Label className="text-[11px] text-ink-muted">Segment</Label>
      <select className="h-8 w-full bg-ground border border-line-strong rounded-sm px-2" value={value.segment_id} onChange={e => set({ segment_id: e.target.value })} data-testid="select-hubspot-segment">
        <option value="">{segs.isLoading ? 'Loading segments…' : 'Choose a segment'}</option>
        {items.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>{err('segment_id')}</div>
    <ScheduleEditor testId="hubspot-dest" value={{ frequency: value.frequency, hour: Number(value.hour), minute: Number(value.minute), day: Number(value.day), timezone: 'America/New_York' }}
      onChange={s => set({ frequency: s.frequency, hour: String(s.hour ?? 0), minute: String(s.minute ?? 0), day: String(s.day ?? 1) })} />
    {err('schedule')}
    {hasToken && !value.replaceToken ? <div className="flex items-center justify-between border border-line rounded-sm p-2 text-[12px]" data-testid="status-hubspot-token-saved"><span>Access token saved (encrypted)</span>
      <Button size="sm" variant="outline" className="h-7" onClick={() => set({ replaceToken: true })} data-testid="button-replace-hubspot-token">Replace</Button></div>
      : <div><Label className="text-[11px] text-ink-muted">Private app access token</Label><Input type="password" autoComplete="new-password" className="h-8 font-mono" value={value.token} onChange={e => set({ token: e.target.value })} data-testid="input-hubspot-token" />{err('token')}
        {hasToken && <Button size="sm" variant="ghost" className="h-7 mt-1" onClick={() => set({ replaceToken: false, token: '' })}>Keep saved token</Button>}</div>}
  </div>;
}

export function HubspotDestinationTools({ d }: { d: Destination }) {
  const test = useHsDestinationTest();
  const run = useHsDestinationRun();
  const [result, setResult] = useState<HsTest | null>(null);
  const [preview, setPreview] = useState(false);
  return <>
    <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" disabled={test.isPending || !d.has_credentials} data-testid={`button-test-destination-${d.id}`}
      onClick={() => test.mutate(d.id, { onSuccess: setResult, onError: e => apiToast(`Test for ${d.name} failed`, e) })}><Plug size={11} /> {test.isPending ? 'Testing…' : 'Test'}</Button>
    <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" disabled={run.isPending || !d.is_active || !d.has_credentials} data-testid={`button-run-destination-${d.id}`}
      onClick={() => run.mutate(d.id, { onSuccess: r => toast({ title: `Run #${r.run_id} ${r.status}`, description: `Job #${r.job_id} queued. Counts appear in the activation run history.` }), onError: e => apiToast(`Run for ${d.name} failed`, e) })}><Play size={11} /> Run</Button>
    <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" onClick={() => setPreview(true)} data-testid={`button-preview-destination-${d.id}`}><Eye size={11} /> Preview</Button>
    <Dialog open={!!result} onOpenChange={o => !o && setResult(null)}><DialogContent className="bg-surface border-line max-w-lg">
      <DialogHeader><DialogTitle className="kin-heading">{d.name} · HubSpot test</DialogTitle><DialogDescription className="text-[12px] text-ink-muted">Checks the saved token and scopes.</DialogDescription></DialogHeader>
      {result && <TestResult r={result} testId={`result-test-${d.id}`} />}</DialogContent></Dialog>
    {preview && <PreviewDialog d={d} onClose={() => setPreview(false)} />}
  </>;
}

function PreviewDialog({ d, onClose }: { d: Destination; onClose: () => void }) {
  const q = useHsDestinationPreview(d.id);
  return <Dialog open onOpenChange={o => !o && onClose()}><DialogContent className="bg-surface border-line max-w-2xl max-h-[90dvh] overflow-y-auto">
    <DialogHeader><DialogTitle className="kin-heading">{d.name} · list preview</DialogTitle><DialogDescription className="text-[12px] text-ink-muted">First {q.data?.limit ?? 100} consented contacts that would be in the list.</DialogDescription></DialogHeader>
    {q.isLoading ? <div className="space-y-2">{[0, 1, 2, 3].map(i => <div key={i} className="h-6 rounded bg-muted animate-pulse" />)}</div>
      : q.isError ? <p role="alert" className="text-danger text-[12px]">{(q.error as Error).message} <button className="underline" onClick={() => q.refetch()}>Retry</button></p>
      : !q.data?.items.length ? <p className="p-4 text-center text-ink-muted text-[12px]" data-testid="empty-hubspot-preview">No consented contacts match this segment.</p>
      : <table className="w-full text-[11px] font-mono" data-testid="table-hubspot-preview"><thead className="text-ink-muted text-left"><tr><th>Master ID</th><th>HubSpot record</th><th>Email</th></tr></thead>
        <tbody>{q.data.items.map(i => <tr key={i.master_id} className="border-t border-line"><td className="py-1 pr-3">{i.master_id}</td><td className="pr-3">{i.hubspot_record_id ?? 'new'}</td><td>{i.email}</td></tr>)}</tbody></table>}
  </DialogContent></Dialog>;
}
