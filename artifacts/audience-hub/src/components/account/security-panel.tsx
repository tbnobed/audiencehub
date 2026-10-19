import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/hooks/use-auth';
import { useAccountActions, useAccountSecurity } from '@/hooks/use-security';

const msg = (e: unknown) => (e instanceof Error ? e.message : 'The request could not be completed.');
const Field = ({ label, children }: { label: string; children: React.ReactNode }) => <label className="flex flex-col gap-1.5 text-xs text-ink-muted">{label}{children}</label>;

export function TotpSection({ enrolling = false }: { enrolling?: boolean }) {
  const sec = useAccountSecurity();
  const { setup, confirm, disable } = useAccountActions();
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const enabled = !!sec.data?.mfa_enabled;
  return <section className="rounded-md border border-line bg-surface">
    <div className="border-b border-line px-5 py-4"><h2 className="kin-heading text-ink flex items-center gap-2"><ShieldCheck size={14} /> Authenticator app (TOTP)</h2>
      <p className="mt-1 text-xs text-ink-muted">{sec.data?.mfa_required ? 'Two-factor authentication is required for your account.' : 'Optional second factor at sign-in.'} Status: <span data-testid="text-mfa-status" className={enabled ? 'text-ok' : ''}>{enabled ? 'Enabled' : 'Not enabled'}</span></p></div>
    <div className="p-5 space-y-4 max-w-md">
      {sec.isLoading && <p className="text-xs text-ink-muted">Loading security status…</p>}
      {sec.isError && <p role="alert" className="text-xs text-danger">{msg(sec.error)} <button className="underline" onClick={() => sec.refetch()}>Retry</button></p>}
      {sec.data && !enabled && !setup.data && <form className="space-y-3" onSubmit={e => { e.preventDefault(); setup.mutate({ password }); setPassword(''); }}>
        <Field label="Confirm your password"><Input data-testid="input-totp-setup-password" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></Field>
        <Button data-testid="button-totp-setup" type="submit" disabled={setup.isPending || !password}>{setup.isPending ? 'Starting…' : 'Set up authenticator'}</Button>
        {setup.isError && <p role="alert" className="text-xs text-danger">{msg(setup.error)}</p>}
      </form>}
      {setup.data && !enabled && <form className="space-y-3" onSubmit={e => { e.preventDefault(); confirm.mutate({ code }, { onSuccess: () => setup.reset() }); setCode(''); }}>
        <p className="text-xs text-ink">Add this account to your authenticator app using the key below (or open the link on your device), then enter the 6-digit code.</p>
        <code data-testid="text-totp-secret" className="block break-all rounded-sm border border-line-strong bg-surface-raised px-3 py-2 font-mono text-xs text-ink">{setup.data.secret}</code>
        <a href={setup.data.uri} className="text-xs text-signal underline break-all">Open in authenticator app</a>
        <Field label="Verification code"><Input data-testid="input-totp-confirm" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} required /></Field>
        <Button data-testid="button-totp-confirm" type="submit" disabled={confirm.isPending || code.length !== 6}>{confirm.isPending ? 'Verifying…' : 'Enable two-factor'}</Button>
        {confirm.isError && <p role="alert" className="text-xs text-danger">{msg(confirm.error)}</p>}
      </form>}
      {enabled && !enrolling && <form className="space-y-3" onSubmit={e => { e.preventDefault(); disable.mutate({ password, code }); setPassword(''); setCode(''); }}>
        <Field label="Password"><Input data-testid="input-totp-disable-password" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></Field>
        <Field label="Current authenticator code"><Input data-testid="input-totp-disable-code" inputMode="numeric" maxLength={6} value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} required /></Field>
        <Button data-testid="button-totp-disable" type="submit" variant="outline" disabled={disable.isPending || !password || code.length !== 6}>{disable.isPending ? 'Disabling…' : 'Disable two-factor'}</Button>
        {sec.data?.mfa_required && <p className="text-xs text-ink-muted">Disabling is rejected while two-factor is required for your role.</p>}
        {disable.isError && <p role="alert" className="text-xs text-danger">{msg(disable.error)}</p>}
      </form>}
    </div>
  </section>;
}

export function PasswordSection() {
  const { password: change } = useAccountActions();
  const { user } = useAuth();
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [totp, setTotp] = useState('');
  const mismatch = again !== '' && next !== again;
  const done = change.isSuccess;
  return <section className="rounded-md border border-line bg-surface">
    <div className="border-b border-line px-5 py-4"><h2 className="kin-heading text-ink">Change password</h2><p className="mt-1 text-xs text-ink-muted">All sessions are signed out after a change, including this one.</p></div>
    {done ? <div className="p-5 space-y-3"><p role="status" className="text-sm text-ok">Password changed. Sign in again with your new password.</p><Button data-testid="button-password-signin" onClick={() => window.location.reload()}>Go to sign in</Button></div>
    : <form className="p-5 space-y-3 max-w-md" onSubmit={e => { e.preventDefault(); if (mismatch) return; change.mutate({ current_password: cur, new_password: next, ...(totp ? { totp } : {}) }); setCur(''); setNext(''); setAgain(''); setTotp(''); }}>
      <Field label="Current password"><Input data-testid="input-current-password" type="password" autoComplete="current-password" value={cur} onChange={e => setCur(e.target.value)} required /></Field>
      <Field label="New password"><Input data-testid="input-new-password" type="password" autoComplete="new-password" value={next} onChange={e => setNext(e.target.value)} required /></Field>
      <Field label="Confirm new password"><Input data-testid="input-confirm-password" type="password" autoComplete="new-password" value={again} onChange={e => setAgain(e.target.value)} required /></Field>
      {mismatch && <p role="alert" className="text-xs text-danger">Passwords do not match.</p>}
      {user?.mfa_enabled && <Field label="Authenticator code"><Input data-testid="input-password-totp" inputMode="numeric" maxLength={6} value={totp} onChange={e => setTotp(e.target.value.replace(/\D/g, ''))} required /></Field>}
      <Button data-testid="button-change-password" type="submit" disabled={change.isPending || !cur || !next || mismatch}>{change.isPending ? 'Changing…' : 'Change password'}</Button>
      {change.isError && <p role="alert" className="text-xs text-danger">{msg(change.error)}</p>}
    </form>}
  </section>;
}
