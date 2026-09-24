import { useState } from 'react';
import { useAuth, Role } from '@/hooks/use-auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useQuery } from '@tanstack/react-query';

export default function Login() {
  const { login } = useAuth();
  const [role, setRole] = useState<Role>('viewer');
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const { data: auth, isLoading } = useQuery<{ mode: 'dev' | 'oidc' }>({
    queryKey: ['auth-mode'],
    queryFn: async () => {
      const response = await fetch('/api/auth-mode');
      if (!response.ok) throw new Error('Cannot reach the authentication service');
      return response.json();
    },
    retry: false,
  });

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
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center space-y-2">
          <CardTitle className="text-2xl font-mono tracking-tight text-primary">AUDIENCE_HUB</CardTitle>
          <CardDescription>{auth?.mode === 'dev' ? 'Select a role to enter the development console.' : 'Sign in to the operations console.'}</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? <p>Checking sign-in settings…</p> : auth?.mode === 'oidc' ? (
            <a href="/auth/login" className="block rounded bg-primary px-4 py-2 text-center text-primary-foreground">Sign in with your organization</a>
          ) : auth?.mode === 'dev' ? <form onSubmit={handleLogin} className="space-y-4">
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
          {errorMessage ? <p role="alert" className="mt-4 text-sm text-destructive">{errorMessage}</p> : null}
        </CardContent>
      </Card>
    </div>
  );
}
