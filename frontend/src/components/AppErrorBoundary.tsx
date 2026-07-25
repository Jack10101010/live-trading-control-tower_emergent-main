import React from 'react';

/**
 * Root failure boundary (UI-0).
 *
 * The shell (SafetyBar / ScopeNavigator / ContextBar / EventDock) uses suspense
 * queries and sits OUTSIDE `RouteErrorBoundary`, so before UI-0 a backend outage
 * unmounted the whole tree and the operator saw a blank page. This boundary is the
 * outermost catch: it never falls back to fixtures, it never shows a stack trace,
 * and it always offers a retry.
 */

export type AppFailureKind =
  | 'backend-unavailable'
  | 'request-failed'
  | 'fixture-unavailable'
  | 'unknown';

interface CopyBlock {
  title: string;
  detail: string;
}

const COPY: Record<AppFailureKind, CopyBlock> = {
  'backend-unavailable': {
    title: 'Backend unreachable',
    detail:
      'The Control Tower interface is running, but its backend did not respond. No trading data is available — nothing shown below should be treated as live.',
  },
  'request-failed': {
    title: 'Backend request failed',
    detail:
      'The backend responded with an error while loading a required view. The Control Tower is running; the data it needs is unavailable.',
  },
  'fixture-unavailable': {
    title: 'Fixture world unavailable',
    detail:
      'This backend serves a fixture world and that fixture could not be loaded. No demonstration data is available.',
  },
  unknown: {
    title: 'Control Tower error',
    detail:
      'An unexpected interface error occurred. The Control Tower is running; this view could not be rendered.',
  },
};

/**
 * Classify a thrown value WITHOUT surfacing its text to the operator.
 * `apiFetch` throws `API {status} {path}: {body}`; a transport failure throws a
 * TypeError ("Failed to fetch" / "Load failed" / NetworkError).
 */
export function classifyFailure(error: unknown): AppFailureKind {
  const msg = error instanceof Error ? error.message : String(error ?? '');
  if (/failed to fetch|load failed|networkerror|err_connection|fetch failed/i.test(msg)) {
    return 'backend-unavailable';
  }
  const api = /^API (\d{3})\s+(\S+)/.exec(msg);
  if (api) {
    const status = Number(api[1]);
    const path = api[2];
    if (status === 404 && /^\/world\b/.test(path)) return 'fixture-unavailable';
    if (status >= 500) return 'request-failed';
    return 'request-failed';
  }
  return 'unknown';
}

interface Props {
  children: React.ReactNode;
  /** Called before the boundary resets (used to clear cached rejections). */
  onRetry?: () => void;
}

interface State {
  kind: AppFailureKind | null;
}

export class AppErrorBoundary extends React.Component<Props, State> {
  state: State = { kind: null };

  static getDerivedStateFromError(error: unknown): State {
    return { kind: classifyFailure(error) };
  }

  componentDidCatch(error: unknown) {
    // Developer detail stays in the console; the operator never sees a trace.
    // eslint-disable-next-line no-console
    console.error('[control-tower] unhandled application error', error);
  }

  private handleRetry = () => {
    this.props.onRetry?.();
    this.setState({ kind: null });
  };

  render() {
    const { kind } = this.state;
    if (!kind) return this.props.children;
    const copy = COPY[kind];
    return (
      <div
        className="fixed inset-0 flex items-center justify-center p-6"
        style={{ background: 'var(--bg)' }}
        data-testid="app-error-state"
        data-failure-kind={kind}
        role="alert"
      >
        <div
          className="max-w-lg w-full rounded-md p-6 flex flex-col gap-4"
          style={{ background: 'var(--panel)', border: '1px solid var(--border)' }}
        >
          {/* Shell context: make clear the Control Tower itself is up. */}
          <div className="flex items-center gap-2">
            <div
              className="w-6 h-6 rounded-sm flex items-center justify-center"
              style={{ background: 'linear-gradient(140deg, #4C82F7 0%, #2FB6C9 100%)' }}
            >
              <span className="text-[9px] font-bold text-white mono">CT</span>
            </div>
            <span className="text-xs uppercase tracking-widest text-text-2">Control Tower</span>
            <span
              className="ml-auto text-2xs mono uppercase px-1.5 py-0.5 rounded-sm"
              style={{ color: 'var(--negative)', border: '1px solid var(--negative)' }}
            >
              Disconnected
            </span>
          </div>

          <div className="flex flex-col gap-1.5">
            <h1 className="text-sm font-semibold text-text">{copy.title}</h1>
            <p className="text-xs text-text-2 leading-relaxed">{copy.detail}</p>
          </div>

          <p className="text-2xs text-text-muted leading-relaxed">
            The Control Tower does not substitute demonstration data when its backend is
            unavailable. Execution safety does not depend on this interface — an execution node
            continues to run and protect the account with the Control Tower closed.
          </p>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={this.handleRetry}
              data-testid="app-error-retry"
              className="text-xs px-3 py-1.5 rounded-sm font-medium"
              style={{ background: 'var(--primary)', color: '#fff' }}
            >
              Retry
            </button>
            <span className="text-2xs text-text-muted mono">{kind}</span>
          </div>
        </div>
      </div>
    );
  }
}
