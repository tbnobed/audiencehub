import { ShieldAlert } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { TotpSection } from '@/components/account/security-panel';
import { useAuth } from '@/hooks/use-auth';

export default function MfaGate() {
  const { logout } = useAuth();
  return <div className="min-h-screen bg-background flex items-start justify-center p-4 pt-16">
    <div className="w-full max-w-lg space-y-4">
      <div role="alert" className="rounded-md border border-line bg-surface p-5">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[.16em] font-mono text-signal mb-1"><ShieldAlert size={13} /> Action required</div>
        <h1 className="kin-title text-ink">Set up two-factor authentication</h1>
        <p className="text-xs text-ink-muted mt-1">Your organization requires an authenticator app for administrators. Complete enrollment to continue to the console.</p>
      </div>
      <TotpSection enrolling />
      <Button data-testid="button-mfa-signout" variant="outline" size="sm" onClick={() => logout()}>Sign out</Button>
    </div>
  </div>;
}
