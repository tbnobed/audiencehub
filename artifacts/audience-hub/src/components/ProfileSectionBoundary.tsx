import { Component, type ErrorInfo, type ReactNode } from 'react';
import { reportClientError } from '@/lib/client-errors';
import { Button } from '@/components/ui/button';

type Props = {
  section: string;
  route: string;
  profileId: string;
  children: () => ReactNode;
  onRetry?: () => unknown;
};
type State = { error: Error | null; componentStack: string };

// Evaluate section expressions below the boundary, not in ProfileDetail's render.
function SectionContent({ children }: Pick<Props, 'children'>) {
  return <>{children()}</>;
}

export class ProfileSectionBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '' };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ componentStack: info.componentStack || '' });
    reportClientError(error, info, this.props.route, this.props.profileId);
  }

  componentDidUpdate(previous: Props) {
    if (previous.profileId !== this.props.profileId && this.state.error) {
      this.setState({ error: null, componentStack: '' });
    }
  }

  render() {
    if (!this.state.error) return <SectionContent>{this.props.children}</SectionContent>;
    return (
      <section role="alert" aria-label={this.props.section} className="rounded border border-destructive/30 p-4 space-y-3">
        <p className="text-sm font-medium">Couldn't load this section</p>
        <Button variant="outline" size="sm" onClick={() => {
          this.setState({ error: null, componentStack: '' });
          this.props.onRetry?.();
        }}>Retry</Button>
        {import.meta.env.DEV && <pre className="text-xs whitespace-pre-wrap overflow-auto">
          {this.state.error.message}{'\n'}{this.state.componentStack}
        </pre>}
      </section>
    );
  }
}