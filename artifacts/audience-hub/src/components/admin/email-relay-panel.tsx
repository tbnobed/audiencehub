import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { fetchApi, API_TIMEOUT_MS } from '@/lib/api';
import { useAuth } from '@/hooks/use-auth';

interface Relay { enabled: boolean; from_email: string; from_name: string; public_url: string; key_configured: boolean }
interface Delivery { id: number; purpose: string; user_id: number | null; created_at: string; accepted_at: string | null; cancelled_at: string | null; expires_at: string; job_status: string | null; attempts: number | null; error: string | null }
const errorText = (e: unknown) => e instanceof Error ? e.message : 'The request could not be completed.';
const Field = ({ label, children }: { label: string; children: React.ReactNode }) => <label className="flex flex-col gap-1.5 text-xs text-ink-muted">{label}{children}</label>;
const request = (method: string, body: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), timeoutMs: API_TIMEOUT_MS });

export function EmailRelayPanel() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const query = useQuery<Relay>({ queryKey: ['email-relay'], queryFn: () => fetchApi('/api/admin/email') });
  const history = useQuery<{ items: Delivery[] }>({ queryKey: ['email-history'], queryFn: () => fetchApi('/api/admin/email/history'), refetchInterval: 10_000, refetchIntervalInBackground: false });
  const [enabled, setEnabled] = useState(false);
  const [email, setEmail] = useState('');
  const [name, setName] = useState('Kinship');
  const [url, setUrl] = useState('');
  const [key, setKey] = useState('');
  const [recipient, setRecipient] = useState(user?.email ?? '');
  useEffect(() => {
    if (!query.data) return;
    setEnabled(query.data.enabled); setEmail(query.data.from_email);
    setName(query.data.from_name); setUrl(query.data.public_url);
  }, [query.data]);
  const save = useMutation({
    mutationFn: (body: Omit<Relay, 'key_configured'> & { api_key?: string }) => {
      // Serialize once and remove the write-only API key from mutation variables.
      const options = request('PUT', body);
      delete body.api_key;
      return fetchApi('/api/admin/email', options);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['email-relay'] }),
  });
  const test = useMutation({
    mutationFn: () => fetchApi('/api/admin/email/test', request('POST', { recipient })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['email-history'] }),
  });
  const retry = useMutation({
    mutationFn: (id: number) => fetchApi(`/api/admin/email/${id}/retry`, request('POST', {})),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['email-history'] }),
  });
  return <section className="rounded-md border border-line bg-surface">
    <div className="border-b border-line px-5 py-4">
      <h2 className="kin-heading text-ink">Account email relay — SendGrid</h2>
      <p className="mt-1 text-xs text-ink-muted">Invitations and password resets. Direct SendGrid HTTPS delivery from your server; no Replit services. Use a verified sender and a restricted API key with Mail Send permission.</p>
    </div>
    <div className="p-5 space-y-4">
      {query.isLoading && <p className="text-xs">Loading relay configuration…</p>}
      {query.isError && <p role="alert" className="text-xs text-danger">{errorText(query.error)} <button className="underline" onClick={() => query.refetch()}>Retry</button></p>}
      {query.data && <form className="space-y-3" onSubmit={e => {
        e.preventDefault();
        save.mutate({ enabled, from_email: email, from_name: name, public_url: url, ...(key ? { api_key: key } : {}) });
        setKey('');
      }}>
        <label className="flex items-center gap-2 text-sm text-ink"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />Enable SendGrid relay</label>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Verified sender email"><Input type="email" autoComplete="off" value={email} onChange={e => { save.reset(); setEmail(e.target.value); }} required /></Field>
          <Field label="Sender name"><Input value={name} maxLength={100} onChange={e => { save.reset(); setName(e.target.value); }} required /></Field>
          <Field label="Public Kinship URL (HTTPS)"><Input type="url" placeholder="https://cdp.example.org" value={url} onChange={e => { save.reset(); setUrl(e.target.value); }} required /></Field>
          <Field label={query.data.key_configured ? 'Replace API key (leave blank to keep existing)' : 'SendGrid API key'}><Input type="password" autoComplete="new-password" value={key} maxLength={512} onChange={e => { save.reset(); setKey(e.target.value); }} required={enabled && !query.data.key_configured} /></Field>
        </div>
        <p className="text-xs text-ink-muted">API key: {query.data.key_configured ? 'saved, encrypted and never returned' : 'not configured'}. Account links use the public URL above, never the request Host header.</p>
        <Button type="submit" disabled={save.isPending}>{save.isPending ? 'Saving…' : 'Save email relay'}</Button>
        {save.isSuccess && <p role="status" className="text-xs text-ok">Relay settings saved.</p>}
        {save.isError && <p role="alert" className="text-xs text-danger">{errorText(save.error)}</p>}
      </form>}
      <form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); test.mutate(); }}>
        <Field label="Test recipient"><Input type="email" value={recipient} onChange={e => { test.reset(); setRecipient(e.target.value); }} required /></Field>
        <Button type="submit" variant="outline" disabled={test.isPending || !query.data?.enabled}>{test.isPending ? 'Queueing…' : 'Send test email'}</Button>
      </form>
      <p className="text-xs text-ink-muted">Save settings before sending a test. The worker must be running. Accepted means SendGrid accepted the message, not confirmed inbox delivery; inspect bounces in SendGrid Email Activity.</p>
      {test.isSuccess && <p role="status" className="text-xs text-ok">Test email queued. Delivery status appears below.</p>}
      {(test.isError || retry.isError) && <p role="alert" className="text-xs text-danger">{errorText(test.error || retry.error)}</p>}
      <details>
        <summary className="text-sm text-ink cursor-pointer">Recent email delivery history</summary>
        {history.isError && <p role="alert" className="text-xs text-danger">{errorText(history.error)}</p>}
        {history.isLoading && <p className="text-xs">Loading delivery history…</p>}
        {history.data?.items.length === 0 && <p className="text-xs text-ink-muted py-3">No messages queued yet.</p>}
        <div className="overflow-auto"><table className="w-full text-xs text-left mt-3">
          <thead><tr className="border-b border-line text-ink-muted"><th>Message</th><th>Purpose / user</th><th>Created</th><th>Status</th><th>Attempts / error</th><th /></tr></thead>
          <tbody>{history.data?.items.map(row => <tr key={row.id} className="border-b border-line align-top">
            <td className="py-2">{row.id}</td><td>{row.purpose} / {row.user_id ?? 'test'}</td>
            <td>{new Date(row.created_at).toLocaleString()}</td>
            <td>{row.accepted_at ? 'Accepted by SendGrid' : row.cancelled_at ? 'Cancelled / expired' : row.job_status ?? 'Unavailable'}</td>
            <td className="max-w-xs">{row.attempts ?? 0}{row.error && !row.accepted_at ? ` — ${row.error}` : ''}</td>
            <td>{!row.accepted_at && !row.cancelled_at && ['failed', 'cancelled'].includes(row.job_status ?? '') && new Date(row.expires_at) > new Date() && <Button size="sm" variant="outline" disabled={retry.isPending || !query.data?.enabled} onClick={() => retry.mutate(row.id)}>Retry</Button>}</td>
          </tr>)}</tbody>
        </table></div>
      </details>
    </div>
  </section>;
}
