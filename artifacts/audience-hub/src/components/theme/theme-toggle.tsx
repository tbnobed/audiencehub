import { Moon, Sun } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { Theme } from '@/lib/theme';
import { cn } from '@/lib/utils';

export function ThemeToggle({ theme, onToggle, className }: { theme: Theme; onToggle: () => void; className?: string }) {
  const next = theme === 'dark' ? 'light' : 'dark';
  const label = `Switch to ${next} theme`;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={onToggle}
      aria-label={label}
      title={label}
      data-testid="button-theme-toggle"
      className={cn('text-ink-muted hover:text-ink focus-visible:text-ink', className)}
    >
      {theme === 'dark' ? <Sun className="h-4 w-4" aria-hidden="true" /> : <Moon className="h-4 w-4" aria-hidden="true" />}
    </Button>
  );
}
