import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';

export function useShortcuts() {
  const navigate = useNavigate();
  const lastKeyRef = useRef<string | null>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't trigger shortcuts if user is typing in an input
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement ||
        (e.target as HTMLElement).isContentEditable
      ) {
        return;
      }

      if (e.key === '/') {
        e.preventDefault();
        const searchInput = document.getElementById('global-search');
        if (searchInput) {
          searchInput.focus();
        }
        return;
      }

      if (e.key === 'g') {
        lastKeyRef.current = 'g';
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(() => {
          lastKeyRef.current = null;
        }, 1000);
        return;
      }

      if (lastKeyRef.current === 'g') {
        switch (e.key) {
          case 'd':
             navigate('/');
            break;
          case 'p':
             navigate('/profiles');
            break;
          case 's':
             navigate('/segments');
            break;
          case 'a':
             navigate('/activations');
            break;
          case 'i':
             navigate('/imports');
            break;
        }
        lastKeyRef.current = null;
        if (timerRef.current) clearTimeout(timerRef.current);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
   }, [navigate]);
}
