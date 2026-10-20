import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { API_TIMEOUT_MS, fetchApi, setCsrfToken } from '@/lib/api';
import { useAuth, Role } from '@/hooks/use-auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useQuery } from '@tanstack/react-query';
import { brandAsset, useTheme } from '@/lib/theme';
import { ThemeToggle } from '@/components/theme/theme-toggle';
import { LoginTotpDialog } from '@/components/account/login-totp-dialog';

async function loginCsrf(signal?: AbortSignal): Promise<string> {
  const data = await fetchApi('/auth/local/session', {
    method: 'GET', cache: 'no-store', signal, timeoutMs: API_TIMEOUT_MS,
  });
  if (typeof data.csrf_token !== 'string' || !data.csrf_token) {
    throw new Error('Could not prepare a secure sign-in session. Please try again.');
  }
  return data.csrf_token;
}

export default function Login() {
  const { login } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [role, setRole] = useState<Role>('viewer');
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const { data: auth, isLoading } = useQuery<{ mode: 'dev' | 'local' }>({
    queryKey: ['auth-mode'],
    queryFn: async () => {
      const response = await fetch('/api/auth-mode');
      if (!response.ok) throw new Error('Cannot reach the authentication service');
      return response.json();
    },
    retry: false,
  });

  const qc = useQueryClient();
  const [token, setToken] = useState(() => new URLSearchParams(window.location.search).get('token'));
  const [notice, setNotice] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [again, setAgain] = useState('');
  const [totpOpen, setTotpOpen] = useState(false);
  const [totpExpires, setTotpExpires] = useState('');
  const [totpError, setTotpError] = useState('');
  const [csrf, setCsrf] = useState('');
  useEffect(() => {
    if (auth?.mode !== 'local') return;
    const controller = new AbortController();
    loginCsrf(controller.signal).then(setCsrf).catch(() => {
      if (!controller.signal.aborted) setErrorMessage('Cannot reach the authentication service');
    });
    return () => controller.abort();
  }, [auth?.mode]);
  const post = async (path: string, body: object) => {
    // Another tab or an expired session may have changed the cookie since mount.
    // Await its matching fresh token/cookie before sending credentials.
    const currentCsrf = await loginCsrf();
    setCsrf(currentCsrf);
    const data = await fetchApi(path, {
      method: 'POST', timeoutMs: API_TIMEOUT_MS,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': currentCsrf },
      body: JSON.stringify(body),
    });
    return data;
  };
  const finish = async (data: { login_required?: boolean }) => {
    if (token) window.history.replaceState(null, '', window.location.pathname);
    if (data.login_required) {
      setToken(null); setPassword(''); setAgain('');
      setNotice('Password set. Sign in with your email and password; your authenticator will be requested next.');
      return;
    }
    setCsrfToken(null);
    await qc.invalidateQueries({ queryKey: ['me'] });
  };
  const handleLocal = async (e: React.FormEvent) => {
    e.preventDefault();
    if (token && password !== again) { setErrorMessage('Passwords do not match.'); return; }
    setLoading(true); setErrorMessage('');
    try {
      const data = token
        ? await post('/auth/local/accept', { token, password })
        : await post('/auth/local/login', { email, password });
      setPassword(''); setAgain('');
      if (data.mfa_required) {
        setNotice(''); setTotpError(''); setTotpExpires(data.expires_at); setTotpOpen(true);
        return;
      }
      await finish(data);
    } catch (error) {
      const m = error instanceof Error ? error.message : 'Sign-in failed';
      setErrorMessage(m);
    } finally { setLoading(false); }
  };

  const verifyTotp = async (code: string) => {
    setLoading(true); setTotpError('');
    try {
      const data = await post('/auth/local/totp', { code });
      await finish(data);
      setTotpOpen(false);
    } catch (error) {
      setTotpError(error instanceof Error ? error.message : 'Verification failed. Try again.');
    } finally { setLoading(false); }
  };
  const cancelTotp = async () => {
    setLoading(true); setTotpError('');
    try {
      await post('/auth/local/totp/cancel', {});
      setTotpOpen(false); setTotpExpires(''); setPassword('');
    } catch (error) {
      setTotpError(error instanceof Error ? error.message : 'Could not cancel. Try again.');
    } finally { setLoading(false); }
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name) return;
    setLoading(true);
    try {
      await login(role, name);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Sign-in failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen bg-background flex items-center justify-center p-4">
      <ThemeToggle theme={theme} onToggle={toggleTheme} className="absolute right-4 top-4" />
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center space-y-2">
          <CardTitle className="sr-only">Sign in to Kinship</CardTitle>
          <img src={brandAsset('stacked', theme)} alt="Kinship" className="mx-auto h-auto w-40" />
          <CardDescription>{auth?.mode === 'dev' ? 'Select a role to enter the development console.' : 'Sign in to the operations console.'}</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? <p>Checking sign-in settings…</p> : auth?.mode === 'local' ? <form onSubmit={handleLocal} className="space-y-4">
            {token ? <p className="text-sm text-muted-foreground">Choose a password to activate your account or finish a reset.</p> : <div className="space-y-2"><label className="text-sm font-medium" htmlFor="login-email">Email</label><Input id="login-email" data-testid="input-login-email" type="email" autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} required /></div>}
            <div className="space-y-2"><label className="text-sm font-medium" htmlFor="login-password">{token ? 'New password' : 'Password'}</label><Input id="login-password" data-testid="input-login-password" type="password" autoComplete={token ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)} required /></div>
            {token && <div className="space-y-2"><label className="text-sm font-medium" htmlFor="login-again">Confirm password</label><Input id="login-again" data-testid="input-login-confirm" type="password" autoComplete="new-password" value={again} onChange={e => setAgain(e.target.value)} required /></div>}
            <Button data-testid="button-login" type="submit" className="w-full" disabled={loading || !csrf || totpOpen}>{loading ? 'Working…' : token ? 'Set password and continue' : 'Sign in'}</Button>
          </form> : auth?.mode === 'dev' ? <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">Name</label>
              <Input 
                value={name} 
                onChange={(e) => setName(e.target.value)} 
                placeholder="e.g. Alice" 
                required 
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Role</label>
              <div className="grid grid-cols-3 gap-2">
                {(['viewer', 'analyst', 'admin'] as const).map(r => (
                  <Button
                    key={r}
                    type="button"
                    variant={role === r ? 'default' : 'outline'}
                    onClick={() => setRole(r)}
                    className="capitalize text-xs"
                  >
                    {r}
                  </Button>
                ))}
              </div>
            </div>
            <Button type="submit" className="w-full" disabled={loading || !name}>
              {loading ? 'Logging in...' : 'Enter Console'}
            </Button>
          </form> : <p className="text-destructive">Authentication is unavailable. Check the API connection.</p>}
          {notice ? <p role="status" className="mt-4 text-sm text-ok">{notice}</p> : null}
          {errorMessage ? <p role="alert" className="mt-4 text-sm text-destructive">{errorMessage}</p> : null}
        </CardContent>
      </Card>
      <LoginTotpDialog open={totpOpen} loading={loading} error={totpError} expiresAt={totpExpires} onVerify={verifyTotp} onCancel={cancelTotp} />
    </div>
  );
}
