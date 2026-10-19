import { useState } from 'react';
import { Copy, Download, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/hooks/use-auth';
import { Link } from 'react-router-dom';
import { EmailRelayPanel } from './email-relay-panel';
import {
  downloadAuditCsv, fetchAuditPage, inviteUrl, useAdminUsers, useMaintenance, useMaintenanceActions, useSecurityPolicy,
  useUpdateSecurityPolicy, useUserActions, type AuditFilters, type AuditItem, type IssuedLink, type MaintenanceConfig,
} from '@/hooks/use-security';

const msg = (e: unknown) => (e instanceof Error ? e.message : 'The request could not be completed.');
const sel = 'rounded-sm border border-line-strong bg-surface-raised px-2 py-1.5 text-xs text-ink';
const when = (v: string | null) => (v ? new Date(v).toLocaleString() : 'Never');
const Box = ({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) => <section className="rounded-md border border-line bg-surface">
  <div className="border-b border-line px-5 py-4"><h2 className="kin-heading text-ink">{title}</h2>{desc && <p className="mt-1 text-xs text-ink-muted">{desc}</p>}</div><div className="p-5">{children}</div></section>;
const Err = ({ e }: { e: unknown }) => <p role="alert" className="text-xs text-danger">{msg(e)}</p>;

function LinkNotice({ link, label, onClose }: { link: IssuedLink; label: string; onClose: () => void }) {
  const url = link.token ? inviteUrl(link.token) : '';
  const [copied, setCopied] = useState(false);
  return <div role="status" data-testid="notice-one-time-link" className="rounded-sm border border-signal/40 bg-signal/5 p-3 space-y-2 mb-4">
    <p className="text-xs text-ink">{label} {link.email_status === 'queued' ? `Email queued as message ${link.message_id}. Check SendGrid delivery history below; queued does not mean delivered.` : 'Email relay is disabled. Share this one-time link securely.'} Expires {when(link.expires_at)}.</p>
    <div className="flex gap-2">{url && <><input readOnly aria-label="One-time link" value={url} onFocus={e => e.currentTarget.select()} className="flex-1 min-w-0 rounded-sm border border-line-strong bg-surface-raised px-2 py-1.5 font-mono text-xs text-ink" />
      <Button data-testid="button-copy-link" size="sm" variant="outline" onClick={() => navigator.clipboard?.writeText(url).then(() => setCopied(true))}><Copy size={12} className="mr-1" />{copied ? 'Copied' : 'Copy'}</Button>
      </>}
      <Button size="sm" variant="ghost" onClick={onClose}>Dismiss</Button></div></div>;
}

export function AdminUsersPanel() {
  const { user: me } = useAuth();
  const users = useAdminUsers();
  const { invite, patch, unlock, reset } = useUserActions();
  const policy = useSecurityPolicy();
  const setPolicy = useUpdateSecurityPolicy();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [role, setRole] = useState('viewer');
  const [link, setLink] = useState<{ link: IssuedLink; label: string } | null>(null);
  const [resetTotp, setResetTotp] = useState<Record<number, boolean>>({});
  const err = invite.error || patch.error || unlock.error || reset.error || setPolicy.error;

  return <div className="space-y-5">
    <Box title="Two-factor policy" desc="Administrators without an authenticator app are held at enrollment until they set one up.">
      <label className="flex items-center gap-2 text-sm text-ink"><input data-testid="toggle-require-admin-totp" type="checkbox" disabled={!policy.data || setPolicy.isPending} checked={!!policy.data?.require_admin_totp} onChange={e => setPolicy.mutate(e.target.checked)} /> Require TOTP for administrators</label>
      {policy.isError && <Err e={policy.error} />}
    </Box>
    <EmailRelayPanel />
    <Box title="Invite user" desc="Sends a single-use setup link through the enabled SendGrid relay. If disabled, a link is shown for secure manual sharing.">
      {link && <LinkNotice link={link.link} label={link.label} onClose={() => { setLink(null); invite.reset(); reset.reset(); }} />}
      <form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); invite.mutate({ email, name, role }, { onSuccess: l => { setLink({ link: l, label: `Invite for ${email}.` }); setEmail(''); setName(''); } }); }}>
        <label className="flex flex-col gap-1.5 text-xs text-ink-muted">Email<Input data-testid="input-invite-email" type="email" value={email} onChange={e => setEmail(e.target.value)} required /></label>
        <label className="flex flex-col gap-1.5 text-xs text-ink-muted">Name<Input data-testid="input-invite-name" value={name} onChange={e => setName(e.target.value)} required /></label>
        <label className="flex flex-col gap-1.5 text-xs text-ink-muted">Role<select data-testid="select-invite-role" className={sel + ' py-2'} value={role} onChange={e => setRole(e.target.value)}><option value="viewer">Viewer</option><option value="analyst">Analyst</option><option value="admin">Admin</option></select></label>
        <Button data-testid="button-invite" type="submit" disabled={invite.isPending}>{invite.isPending ? 'Inviting…' : 'Create invite'}</Button>
      </form>
    </Box>
    <Box title="Users">
      {err && <div className="mb-3"><Err e={err} /></div>}
      {users.isLoading ? <p className="text-xs text-ink-muted">Loading users…</p>
        : users.isError ? <div><Err e={users.error} /><Button size="sm" variant="outline" className="mt-2" onClick={() => users.refetch()}><RefreshCw size={12} className="mr-1" />Retry</Button></div>
        : !users.data?.items.length ? <p className="text-xs text-ink-muted">No users yet. Invite the first teammate above.</p>
        : <div className="overflow-x-auto"><table className="w-full text-xs text-ink">
          <thead><tr className="text-left text-ink-muted border-b border-line"><th className="py-2 pr-3">User</th><th className="pr-3">Role</th><th className="pr-3">Status</th><th className="pr-3">Last sign-in</th><th>Actions</th></tr></thead>
          <tbody>{users.data.items.map(u => {
            const locked = !!u.locked_until && new Date(u.locked_until) > new Date();
            const self = u.id === me?.id;
            return <tr key={u.id} data-testid={`row-user-${u.id}`} className="border-b border-line align-top">
              <td className="py-2 pr-3"><div className="font-medium">{u.name}</div><div className="text-ink-muted">{u.email}</div></td>
              <td className="pr-3"><select aria-label={`Role for ${u.email}`} data-testid={`select-role-${u.id}`} className={sel} value={u.role} disabled={self || patch.isPending} onChange={e => patch.mutate({ id: u.id, role: e.target.value })}><option value="viewer">Viewer</option><option value="analyst">Analyst</option><option value="admin">Admin</option></select></td>
              <td className="pr-3 space-y-0.5"><div className={u.is_active ? 'text-ok' : 'text-danger'}>{u.is_active ? 'Active' : 'Disabled'}</div>{locked && <div className="text-danger">Locked until {when(u.locked_until)}</div>}<div className="text-ink-muted">{u.password_set ? 'Password set' : 'Invite pending'} / {u.mfa_enabled ? 'TOTP on' : 'TOTP off'}</div></td>
              <td className="pr-3">{when(u.last_login_at)}</td>
              <td><div className="flex flex-wrap items-center gap-1.5">
                <Button size="sm" variant="outline" data-testid={`button-toggle-active-${u.id}`} disabled={self || patch.isPending} onClick={() => patch.mutate({ id: u.id, is_active: !u.is_active })}>{u.is_active ? 'Disable' : 'Enable'}</Button>
                {locked && <Button size="sm" variant="outline" data-testid={`button-unlock-${u.id}`} disabled={unlock.isPending} onClick={() => unlock.mutate(u.id)}>Unlock</Button>}
                <Button size="sm" variant="outline" data-testid={`button-reset-${u.id}`} disabled={reset.isPending} onClick={() => reset.mutate({ id: u.id, reset_totp: !!resetTotp[u.id] }, { onSuccess: l => setLink({ link: l, label: `Password reset for ${u.email}.` }) })}>Reset password</Button>
                <label className="flex items-center gap-1 text-ink-muted"><input type="checkbox" data-testid={`check-reset-totp-${u.id}`} checked={!!resetTotp[u.id]} onChange={e => setResetTotp(s => ({ ...s, [u.id]: e.target.checked }))} />Also reset TOTP</label>
              </div></td></tr>;
          })}</tbody></table></div>}
      <p className="mt-3 text-xs text-ink-muted">Your own role and status cannot be changed here. Manage your credentials under <Link to="/account" className="text-signal underline">Account security</Link>.</p>
    </Box>
  </div>;
}

const emptyF: AuditFilters = { user_id: '', action: '', object: '', from_date: '', to_date: '' };

export function AuditPanel() {
  const [f, setF] = useState<AuditFilters>(emptyF);
  const [applied, setApplied] = useState<AuditFilters>(emptyF);
  const [rows, setRows] = useState<AuditItem[] | null>(null);
  const [cursor, setCursor] = useState<number | string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState<number | null>(null);

  const load = async (filters: AuditFilters, more = false) => {
    setLoading(true); setError('');
    try {
      const page = await fetchAuditPage(filters, more && cursor ? Number(cursor) : undefined);
      setRows(r => (more && r ? [...r, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (e) { setError(msg(e)); } finally { setLoading(false); }
  };
  const apply = (e: React.FormEvent) => { e.preventDefault(); setApplied(f); load(f); };
  const set = (k: keyof AuditFilters) => (e: React.ChangeEvent<HTMLInputElement>) => setF(s => ({ ...s, [k]: e.target.value }));
  const lbl = 'flex flex-col gap-1 text-xs text-ink-muted';

  return <Box title="Audit log" desc="Search administrative and security events. Object accepts a type (segment) or type:id (segment:42).">
    <form onSubmit={apply} className="flex flex-wrap items-end gap-3 mb-4">
      <label className={lbl}>User ID<Input data-testid="filter-audit-user" inputMode="numeric" className="w-24" value={f.user_id} onChange={set('user_id')} /></label>
      <label className={lbl}>Action<Input data-testid="filter-audit-action" value={f.action} onChange={set('action')} /></label>
      <label className={lbl}>Object<Input data-testid="filter-audit-object" value={f.object} onChange={set('object')} /></label>
      <label className={lbl}>From<input data-testid="filter-audit-from" type="date" className={sel + ' py-2'} value={f.from_date} onChange={set('from_date')} /></label>
      <label className={lbl}>To<input data-testid="filter-audit-to" type="date" className={sel + ' py-2'} value={f.to_date} onChange={set('to_date')} /></label>
      <Button data-testid="button-audit-search" type="submit" disabled={loading}>{loading && !rows ? 'Searching…' : 'Search'}</Button>
      <Button data-testid="button-audit-clear" type="button" variant="outline" onClick={() => { setF(emptyF); setApplied(emptyF); load(emptyF); }}>Clear</Button>
      <Button data-testid="button-audit-export" type="button" variant="outline" onClick={() => downloadAuditCsv(applied).catch(e => setError(msg(e)))}><Download size={13} className="mr-1" />Download CSV</Button>
    </form>
    {error && <div className="mb-3"><Err e={error} /></div>}
    {rows === null ? <p className="text-xs text-ink-muted">{loading ? 'Loading…' : 'Set filters and search to browse the audit log. CSV export uses the last applied filters.'}</p>
      : rows.length === 0 ? <p className="text-xs text-ink-muted">No audit events match these filters.</p>
      : <div className="overflow-x-auto"><table className="w-full text-xs text-ink">
        <thead><tr className="text-left text-ink-muted border-b border-line"><th className="py-2 pr-3">When</th><th className="pr-3">Actor</th><th className="pr-3">Action</th><th className="pr-3">Object</th><th>Details</th></tr></thead>
        <tbody>{rows.map(r => <tr key={r.id} data-testid={`row-audit-${r.id}`} className="border-b border-line align-top">
          <td className="py-2 pr-3 whitespace-nowrap">{when(r.at)}</td><td className="pr-3">{r.actor_type}{r.user_id != null ? ` #${r.user_id}` : ''}</td>
          <td className="pr-3 font-mono">{r.action}</td><td className="pr-3 font-mono">{r.entity_type}{r.entity_id ? `:${r.entity_id}` : ''}</td>
          <td>{r.details ? <button className="text-signal underline" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? 'Hide' : 'Show'}</button> : '-'}
            {open === r.id && <pre className="mt-1 max-w-sm overflow-auto whitespace-pre-wrap font-mono text-[11px] text-ink-muted">{JSON.stringify(r.details, null, 2)}</pre>}</td></tr>)}</tbody></table></div>}
    {rows && cursor != null && <Button data-testid="button-audit-more" className="mt-3" variant="outline" size="sm" disabled={loading} onClick={() => load(applied, true)}>{loading ? 'Loading…' : 'Load more'}</Button>}
  </Box>;
}

const fields: { key: keyof MaintenanceConfig; label: string }[] = [
  { key: 'event_retention_days', label: 'Event retention (days)' }, { key: 'audit_retention_days', label: 'Audit retention (days)' },
  { key: 'job_retention_days', label: 'Job retention (days)' }, { key: 'enrichment_retention_days', label: 'Enrichment retention (days)' },
  { key: 'partitions_ahead_months', label: 'Partitions ahead (months)' },
];

function MaintenanceForm({ config }: { config: MaintenanceConfig }) {
  const { save } = useMaintenanceActions();
  const [v, setV] = useState<Record<string, string>>(() => Object.fromEntries(fields.map(x => [x.key, String(config[x.key])])));
  const bad = fields.some(x => !/^\d+$/.test(v[x.key]));
  const dirty = fields.some(x => Number(v[x.key]) !== config[x.key]);
  return <form className="space-y-3" onSubmit={e => { e.preventDefault(); if (bad) return; save.mutate(Object.fromEntries(fields.map(x => [x.key, Number(v[x.key])])) as unknown as MaintenanceConfig); }}>
    <div className="flex flex-wrap gap-3">{fields.map(x => <label key={x.key} className="flex flex-col gap-1 text-xs text-ink-muted">{x.label}<Input data-testid={`input-${x.key}`} inputMode="numeric" className="w-40" value={v[x.key]} onChange={e => { save.reset(); setV(s => ({ ...s, [x.key]: e.target.value })); }} /></label>)}</div>
    <p className="text-xs text-ink-muted">A retention of 0 disables deletion for that category. Defaults: events 730, audit 0, jobs 90, enrichment 0, partitions ahead 3.</p>
    {bad && <p role="alert" className="text-xs text-danger">Enter whole numbers of 0 or more.</p>}
    <div className="flex items-center gap-3"><Button data-testid="button-save-maintenance" type="submit" disabled={bad || !dirty || save.isPending}>{save.isPending ? 'Saving…' : 'Save maintenance settings'}</Button>{save.isSuccess && <span role="status" className="text-xs text-ok">Saved.</span>}</div>
    {save.isError && <Err e={save.error} />}
  </form>;
}

export function MaintenancePanel() {
  const m = useMaintenance();
  const { run } = useMaintenanceActions();
  return <div className="space-y-5">
    <Box title="Retention and partitions">
      {m.isLoading ? <p className="text-xs text-ink-muted">Loading maintenance settings…</p>
        : m.isError ? <div><Err e={m.error} /><Button size="sm" variant="outline" className="mt-2" onClick={() => m.refetch()}><RefreshCw size={12} className="mr-1" />Retry</Button></div>
        : m.data && <MaintenanceForm key={JSON.stringify(m.data.config)} config={m.data.config} />}
    </Box>
    <Box title="Run history" desc="Maintenance runs on a schedule. Start one manually when needed.">
      <div className="flex items-center gap-3 mb-4"><Button data-testid="button-run-maintenance" variant="outline" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? 'Queuing…' : 'Run maintenance now'}</Button>{run.isSuccess && <span role="status" className="text-xs text-ok">Queued job #{run.data.job_id}.</span>}</div>
      {run.isError && <Err e={run.error} />}
      {m.data && (m.data.runs.length === 0 ? <p className="text-xs text-ink-muted">No maintenance runs recorded yet.</p>
        : <div className="overflow-x-auto"><table className="w-full text-xs text-ink"><thead><tr className="text-left text-ink-muted border-b border-line"><th className="py-2 pr-3">Run</th><th className="pr-3">Status</th><th className="pr-3">Started</th><th className="pr-3">Finished</th><th>Result</th></tr></thead>
          <tbody>{m.data.runs.map(r => <tr key={r.id} data-testid={`row-maintenance-${r.id}`} className="border-b border-line align-top"><td className="py-2 pr-3">#{r.id}</td><td className={'pr-3 ' + (r.error || r.status === 'failed' ? 'text-danger' : r.status === 'succeeded' || r.status === 'completed' ? 'text-ok' : '')}>{r.status}</td><td className="pr-3">{when(r.created_at)}</td><td className="pr-3">{when(r.finished_at)}</td>
            <td>{r.error ? <span className="text-danger">{r.error}</span> : r.progress || r.payload ? <pre className="max-w-xs overflow-auto whitespace-pre-wrap font-mono text-[11px] text-ink-muted">{JSON.stringify(r.progress || r.payload)}</pre> : '-'}</td></tr>)}</tbody></table></div>)}
    </Box>
  </div>;
}
