import { KeyRound, ShieldAlert } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Textarea } from '@/components/ui/textarea';
import type { SftpForm } from './logic';

/** SFTP destination fields. Credentials are write-only: never prefilled, never echoed back. */
export function SftpFields({ value: s, onChange, includePii, onIncludePii, hasCredentials, editing, errors }: {
  value: SftpForm; onChange: (v: SftpForm) => void; includePii: boolean; onIncludePii: (v: boolean) => void;
  hasCredentials: boolean; editing: boolean; errors: Record<string, string>;
}) {
  const up = (patch: Partial<SftpForm>) => onChange({ ...s, ...patch });
  const err = (k: string) => errors[k] ? <p className="text-danger text-[11px] mt-0.5" data-testid={`error-${k}`}>{errors[k]}</p> : null;
  const crosswalk = s.export_kind === 'crosswalk';
  return <div className="space-y-3" data-testid="section-sftp">
    <div className="grid grid-cols-2 gap-3">
      <div><Label htmlFor="sftp-dir" className="text-[11px] text-ink-muted">Output directory</Label>
        <Input id="sftp-dir" className="h-8 font-mono" value={s.output_dir} onChange={e => up({ output_dir: e.target.value })} data-testid="input-sftp-output-dir" />{err('output_dir')}
        <p className="text-[11px] text-ink-muted mt-0.5">Each run writes run-N/ with manifest.json last.</p></div>
      <div><Label className="text-[11px] text-ink-muted">Export kind</Label>
        <div className="grid grid-cols-2 gap-1 mt-0.5">{(['audience', 'crosswalk'] as const).map(k =>
          <button key={k} type="button" onClick={() => up({ export_kind: k })} data-testid={`button-export-kind-${k}`}
            className={`h-8 rounded-sm border text-[12px] ${s.export_kind === k ? 'border-signal bg-signal-soft text-ink' : 'border-line text-ink-muted hover:border-line-strong'}`}>{k}</button>)}</div></div>
    </div>
    <div className="flex flex-wrap gap-4 text-[12px]">
      <label className="flex items-center gap-1.5"><Checkbox checked={s.gzip} onCheckedChange={v => up({ gzip: !!v })} data-testid="checkbox-sftp-gzip" /> gzip files</label>
      <label className="flex items-center gap-1.5"><Checkbox checked={crosswalk || includePii} disabled={crosswalk} onCheckedChange={v => onIncludePii(!!v)} data-testid="checkbox-sftp-include-pii" /> Include PII {crosswalk && <span className="text-ink-muted">(required for crosswalk)</span>}</label>
      <span className="text-ink-muted">Delimiter: comma (fixed)</span>
    </div>
    {crosswalk && <div className="border border-warn/40 bg-warn/5 rounded-sm p-2 space-y-1">
      <Label htmlFor="sftp-purpose" className="text-[12px] text-ink inline-flex items-center gap-1"><ShieldAlert size={12} className="text-warn" /> Approved purpose (admin)</Label>
      <Textarea id="sftp-purpose" rows={2} className="text-[12px]" placeholder="e.g. Daily master_id ↔ HubSpot ID crosswalk for CustomerPulse matching, approved by data governance" value={s.approved_purpose} onChange={e => up({ approved_purpose: e.target.value })} data-testid="input-sftp-approved-purpose" />{err('approved_purpose')}
      <p className="text-[11px] text-ink-muted">Crosswalk files contain identifiers and PII. Activations using this destination are admin-only and must name a consent channel.</p>
    </div>}
    <div className="border border-line rounded-sm p-2 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-ink-muted inline-flex items-center gap-1"><KeyRound size={11} /> Key-auth credentials {hasCredentials ? <span className="text-ok">(on file)</span> : <span className="text-warn">(none on file)</span>}</span>
        {editing && hasCredentials && <label className="flex items-center gap-1.5 text-[11px]"><Checkbox checked={s.replaceCredentials} onCheckedChange={v => up({ replaceCredentials: !!v, private_key: '', passphrase: '' })} data-testid="checkbox-replace-credentials" /> Replace credentials</label>}
      </div>
      {s.replaceCredentials ? <>
        <div className="grid grid-cols-[1fr_80px_1fr] gap-2">
          <div><Label htmlFor="sftp-host" className="text-[11px] text-ink-muted">Host</Label><Input id="sftp-host" autoComplete="off" className="h-8 font-mono" value={s.host} onChange={e => up({ host: e.target.value })} data-testid="input-sftp-host" />{err('host')}</div>
          <div><Label htmlFor="sftp-port" className="text-[11px] text-ink-muted">Port</Label><Input id="sftp-port" inputMode="numeric" className="h-8 font-mono" value={s.port} onChange={e => up({ port: e.target.value })} data-testid="input-sftp-port" />{err('port')}</div>
          <div><Label htmlFor="sftp-user" className="text-[11px] text-ink-muted">Username</Label><Input id="sftp-user" autoComplete="off" className="h-8 font-mono" value={s.username} onChange={e => up({ username: e.target.value })} data-testid="input-sftp-username" />{err('username')}</div>
        </div>
        <div><Label htmlFor="sftp-key" className="text-[11px] text-ink-muted">Private key</Label>
          <Textarea id="sftp-key" rows={4} autoComplete="off" spellCheck={false} className="font-mono text-[11px] [-webkit-text-security:disc]" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" value={s.private_key} onChange={e => up({ private_key: e.target.value })} data-testid="input-sftp-private-key" />{err('private_key')}</div>
        <div className="grid grid-cols-2 gap-2">
          <div><Label htmlFor="sftp-pass" className="text-[11px] text-ink-muted">Key passphrase (optional)</Label><Input id="sftp-pass" type="password" autoComplete="new-password" className="h-8 font-mono" value={s.passphrase} onChange={e => up({ passphrase: e.target.value })} data-testid="input-sftp-passphrase" /></div>
          <div><Label htmlFor="sftp-hostkey" className="text-[11px] text-ink-muted">Host key SHA256 pin</Label><Input id="sftp-hostkey" autoComplete="off" className="h-8 font-mono text-[11px]" placeholder="SHA256:…" value={s.host_key_sha256} onChange={e => up({ host_key_sha256: e.target.value })} data-testid="input-sftp-host-key" />{err('host_key_sha256')}</div>
        </div>
        <p className="text-[11px] text-ink-muted">Write-only, encrypted at rest, never returned. {editing && hasCredentials ? 'Saving replaces the full credential set.' : ''}</p>
      </> : <p className="text-[11px] text-ink-muted">Existing credentials are preserved.</p>}
    </div>
  </div>;
}
