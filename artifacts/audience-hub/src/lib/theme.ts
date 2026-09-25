import { useCallback, useEffect, useState } from 'react';

export type Theme = 'dark' | 'light';
export const THEME_STORAGE_KEY = 'kinship-theme';
export const DEFAULT_THEME: Theme = 'dark';
const THEME_COLORS: Record<Theme, string> = { dark: '#0B0F14', light: '#F4F6F9' };

export function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light';
}

export function readStoredTheme(storage: Pick<Storage, 'getItem'> | undefined = safeStorage()): Theme {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export const THEME_CHANGE_EVENT = 'kinship-theme-change';

export function storeTheme(theme: Theme, storage: Pick<Storage, 'setItem'> | undefined = safeStorage()): void {
  try { storage?.setItem(THEME_STORAGE_KEY, theme); } catch { /* storage unavailable (private mode / blocked) */ }
}

function broadcast(theme: Theme) {
  window.dispatchEvent(new CustomEvent<Theme>(THEME_CHANGE_EVENT, { detail: theme }));
}

export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  root.classList.remove('dark', 'light');
  root.classList.add(theme);
  root.style.colorScheme = theme;
  root.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[theme]);
}

function safeStorage(): Storage | undefined {
  try { return typeof window !== 'undefined' ? window.localStorage : undefined; } catch { return undefined; }
}

function currentTheme(): Theme {
  if (typeof document !== 'undefined') {
    const root = document.documentElement;
    if (root.classList.contains('light')) return 'light';
    if (root.classList.contains('dark')) return 'dark';
  }
  return readStoredTheme();
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(currentTheme);

  useEffect(() => { applyTheme(theme); }, [theme]);

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_STORAGE_KEY) setThemeState(isTheme(e.newValue) ? e.newValue : DEFAULT_THEME);
    };
    const onLocal = (e: Event) => { const t = (e as CustomEvent<Theme>).detail; if (isTheme(t)) setThemeState(t); };
    window.addEventListener('storage', onStorage);
    window.addEventListener(THEME_CHANGE_EVENT, onLocal);
    return () => { window.removeEventListener('storage', onStorage); window.removeEventListener(THEME_CHANGE_EVENT, onLocal); };
  }, []);

  const setTheme = useCallback((next: Theme) => { storeTheme(next); applyTheme(next); broadcast(next); }, []);
  const toggleTheme = useCallback(() => { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }, [setTheme]);

  return { theme, setTheme, toggleTheme };
}

/** Brand asset path for the active theme. Geometry is identical; only colors differ. */
export function brandAsset(name: 'mark' | 'wordmark' | 'stacked', theme: Theme): string {
  const files = {
    mark: { dark: 'kinship-mark-dark.svg', light: 'kinship-mark-light.svg' },
    wordmark: { dark: 'kinship-sidebar-wordmark.svg', light: 'kinship-sidebar-wordmark-light.svg' },
    stacked: { dark: 'kinship-logo-stacked-dark.svg', light: 'kinship-logo-stacked-light.svg' },
  } as const;
  return `${import.meta.env.BASE_URL}brand/${files[name][theme]}`;
}
