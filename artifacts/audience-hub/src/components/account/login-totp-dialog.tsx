import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';

interface Props {
  open: boolean;
  loading: boolean;
  error: string;
  expiresAt: string;
  onVerify: (code: string) => Promise<void>;
  onCancel: () => Promise<void>;
}

export function LoginTotpDialog({ open, loading, error, expiresAt, onVerify, onCancel }: Props) {
  const [code, setCode] = useState('');
  useEffect(() => { if (!open) setCode(''); }, [open]);
  return <Dialog open={open} onOpenChange={value => { if (!value && !loading) void onCancel(); }}>
    <DialogContent className="sm:max-w-sm" onInteractOutside={event => event.preventDefault()}>
      <DialogHeader>
        <DialogTitle>Verify your sign-in</DialogTitle>
        <DialogDescription>Your password was accepted. Enter the code from your authenticator app to finish signing in.</DialogDescription>
      </DialogHeader>
      <form className="space-y-4" onSubmit={event => {
        event.preventDefault();
        if (loading || !code) return;
        void onVerify(code);
        setCode('');
      }}>
        <div className="space-y-2">
          <label className="text-sm font-medium" htmlFor="login-totp">Authenticator or recovery code</label>
          <Input id="login-totp" data-testid="input-login-totp" autoFocus autoComplete="one-time-code"
            maxLength={19} value={code} onChange={event => setCode(event.target.value.trim())}
            disabled={loading} required aria-invalid={!!error} />
          <p className="text-xs text-muted-foreground">Use any standard TOTP app, or one unused recovery code if your device is unavailable.</p>
          {expiresAt && <p className="text-xs text-muted-foreground">This sign-in request expires at {new Date(expiresAt).toLocaleTimeString()}.</p>}
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" disabled={loading} onClick={() => void onCancel()}>Cancel</Button>
          <Button data-testid="button-verify-totp" type="submit" disabled={loading || !code}>{loading ? 'Working…' : 'Verify and sign in'}</Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>;
}
