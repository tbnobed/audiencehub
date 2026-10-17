import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useQuery } from '@tanstack/react-query';
import { useConsentChange, usePartnerConsents } from '@/hooks/use-privacy';
import type { ConsentChannel, ConsentStatus, PartnerConsent } from '@/lib/privacy';
import { CHANNEL_LABEL, CONSENT_CHANNELS, STATUS_LABEL, canEditConsent, consentNoteError, errMsg, fmtTime } from '@/lib/privacy-ui';

const tone: Record<ConsentStatus, string> = { opted_in: 'text-ok', opted_out: 'text-danger', unknown: 'text-ink-muted' };

export function ConsentEditor({ partnerId }: { partnerId: number }) {
  // Read the cached session (['me'], owned by AuthProvider) without fetching, so the panel also renders outside the provider.
  const { data: user } = useQuery<{ role?: string } | null>({ queryKey: ['me'], enabled: false, queryFn: () => null });
  const q = usePartnerConsents(partnerId);
  const change = useConsentChange(partnerId);
  const editable = canEditConsent(user?.role);
  const [channel, setChannel] = useState<ConsentChannel>('email');
  const [status, setStatus] = useState<ConsentStatus>('opted_out');
  const [note, setNote] = useState('');
  const [touched, setTouched] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<PartnerConsent | null>(null);
  const noteErr = touched ? consentNoteError(note, status) : null;
  const byChannel = new Map((q.data?.items ?? []).map(c => [c.channel, c]));

  const save = () => change.mutate({ channel, status, note: note.trim() }, { onSuccess: r => { setResult(r); setNote(''); setTouched(false); } });

  return <div data-testid="panel-consent-editor" className="space-y-3">
    {q.isLoading ? <div className="space-y-1.5">{CONSENT_CHANNELS.map(c => <div key={c} className="h-5 rounded-sm bg-surface-raised animate-pulse" />)}</div>
      : q.isError ? <div role="alert" className="text-xs"><p className="text-danger">Consent could not be loaded.</p><Button variant="outline" size="sm" className="mt-2" onClick={() => q.refetch()}><RefreshCw size={13} className="mr-2" />Retry</Button></div>
      : <table className="w-full text-xs"><thead className="text-[11px] text-ink-muted"><tr><th className="text-left font-medium py-1">Channel</th><th className="text-left font-medium py-1">Status</th><th className="text-left font-medium py-1">Source ID</th><th className="text-right font-medium py-1">Captured</th></tr></thead>
        <tbody>{CONSENT_CHANNELS.map(c => { const v = byChannel.get(c); const s = v?.status ?? 'unknown'; return <tr key={c} data-testid={`row-consent-${c}`} className="border-t border-line"><td className="py-1.5">{CHANNEL_LABEL[c]}</td><td className={`py-1.5 font-mono ${tone[s]}`}>{STATUS_LABEL[s]}</td><td className="py-1.5 font-mono text-ink-muted">{v?.source_id ?? '—'}</td><td className="py-1.5 text-right font-mono text-ink-muted">{fmtTime(v?.captured_at)}</td></tr>; })}</tbody></table>}

    {!editable ? <p data-testid="text-consent-readonly" className="text-[11px] text-ink-muted">Read only. Only admins can record manual consent changes.</p>
      : <form className="border-t border-line pt-3 space-y-2" onSubmit={e => { e.preventDefault(); setTouched(true); if (!consentNoteError(note, status)) setConfirm(true); }}>
        <p className="text-[11px] uppercase tracking-wide text-ink-muted">Manual change</p>
        <div className="flex flex-wrap gap-2">
          <select aria-label="Channel" data-testid="select-consent-channel" value={channel} onChange={e => { change.reset(); setResult(null); setChannel(e.target.value as ConsentChannel); }} className="rounded-sm border border-line-strong bg-surface-raised px-2 py-1.5 text-xs">{CONSENT_CHANNELS.map(c => <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>)}</select>
          <select aria-label="New status" data-testid="select-consent-status" value={status} onChange={e => { change.reset(); setResult(null); setStatus(e.target.value as ConsentStatus); }} className="rounded-sm border border-line-strong bg-surface-raised px-2 py-1.5 text-xs"><option value="opted_out">Opted out</option><option value="opted_in">Opted in</option><option value="unknown">Unknown</option></select>
        </div>
        <label className="flex flex-col gap-1 text-xs text-ink-muted">Note (required){status === 'opted_in' && ' - describe the explicit authorization'}
          <textarea data-testid="input-consent-note" rows={2} value={note} aria-invalid={!!noteErr} aria-describedby={noteErr ? 'consent-note-error' : undefined} onChange={e => setNote(e.target.value)} className="rounded-sm border border-line-strong bg-surface-raised px-2 py-1.5 text-xs text-ink" />
        </label>
        {noteErr && <p id="consent-note-error" role="alert" className="text-xs text-danger">{noteErr}</p>}
        <Button data-testid="button-save-consent" type="submit" size="sm" disabled={change.isPending} className="bg-signal text-on-signal hover:bg-signal/90">{change.isPending ? 'Saving…' : 'Record change'}</Button>
        {result && <p role="status" data-testid="status-consent-result" className="text-xs text-ink">Server recorded {CHANNEL_LABEL[result.channel]}: <span className={`font-mono ${tone[result.status]}`}>{STATUS_LABEL[result.status]}</span>{result.status !== status && ' (precedence rules kept a different status)'}.</p>}
        {change.isError && <p role="alert" className="text-xs text-danger">Consent was not changed: {errMsg(change.error)}</p>}
      </form>}

    <AlertDialog open={confirm} onOpenChange={setConfirm}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Set {CHANNEL_LABEL[channel]} to {STATUS_LABEL[status]}?</AlertDialogTitle><AlertDialogDescription>{status === 'opted_in' ? 'Confirm you hold explicit authorization from this partner. Opt-outs from other sources may still take precedence.' : 'This manual change is audited with your note.'}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction data-testid="button-confirm-consent" onClick={save}>Record change</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
  </div>;
}
