import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * RouteErrorBoundary — isolates a routed view so a throw inside one page (e.g. a
 * disposed-chart assertion during Market Data teardown) degrades to a local
 * fallback instead of tearing down the whole React tree. This is the structural
 * guarantee that a page render error can never break navigation or blank the app.
 *
 * It resets automatically when `resetKey` changes (wired to the route pathname),
 * so navigating to a different route always clears a prior page's error.
 */
interface Props {
  resetKey: string;
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: Props) {
    // Route changed → clear any error so the new page renders normally.
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface for diagnostics; the app stays alive.
    console.error('[route-error-boundary] caught render error:', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          className="h-full w-full flex flex-col items-center justify-center gap-3 p-6 text-center"
          data-testid="route-error-fallback"
        >
          <div className="text-sm text-text-2 mono">This view hit an error</div>
          <div className="text-2xs text-text-muted max-w-md break-words">{this.state.error.message}</div>
          <div className="text-2xs text-text-muted">
            Navigation still works — pick another page in the sidebar, or reload.
          </div>
          <button
            onClick={() => this.setState({ error: null })}
            className="mt-1 h-6 px-3 text-2xs rounded-sm"
            style={{ color: 'var(--text)', background: 'var(--panel-2)', border: '1px solid var(--border)' }}
          >
            Retry this view
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
