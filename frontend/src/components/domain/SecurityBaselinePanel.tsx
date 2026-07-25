/**
 * UI-9 — security baseline status (read-only diagnostics).
 *
 * Shows ONLY whether each security variable is configured, missing or invalid.
 * It never renders a value — not a token, not a certificate path, not an endpoint
 * — because a path or hostname is still deployment intelligence even though it is
 * not a credential. The backend never sends values, so there is nothing here to
 * accidentally display.
 *
 * The headline is the point of the panel: NOT ACTIVE. Reading configuration is not
 * the same as using it, and nothing in this build can open a connection.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';

const STATUS_COLOR: Record<string, string> = {
  configured: 'var(--text-2)',
  missing: 'var(--text-muted)',
  invalid: 'var(--negative)',
};

export function SecurityBaselinePanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['security-config'],
    queryFn: () => api.securityConfig(),
    staleTime: 60_000,
    retry: false,
  });

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Security baseline"
      data-testid="security-baseline-panel"
    >
      <header className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">
          Security baseline
        </h3>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: 'var(--text-muted)', color: 'var(--text-muted)' }}
          data-testid="security-active-state"
        >
          {data?.active ? 'ACTIVE' : 'NOT ACTIVE'}
        </span>
      </header>

      {isError && (
        <div className="text-2xs text-text-muted">
          Security configuration status unavailable — the backend could not be reached.
        </div>
      )}

      {isLoading && <div className="text-2xs text-text-muted">Reading configuration…</div>}

      {data && (
        <>
          <p className="text-2xs text-text-muted">{data.activeReason}</p>
          <dl
            className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 mt-2 text-2xs"
            data-testid="security-variables"
          >
            {Object.entries(data.variables).map(([name, info]) => (
              <div key={name} className="contents">
                <dt className="mono text-text-muted">
                  {name}
                  {info.secret && (
                    <span className="ml-1 text-text-muted">(secret — never displayed)</span>
                  )}
                </dt>
                <dd className="mono" style={{ color: STATUS_COLOR[info.status] }}>
                  {info.status}
                </dd>
              </div>
            ))}
          </dl>

          {data.findings.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-2xs" data-testid="security-findings">
              {data.findings.map((finding) => (
                <li
                  key={`${finding.severity}:${finding.variable}:${finding.message}`}
                  style={{
                    color:
                      finding.severity === 'error' ? 'var(--negative)' : 'var(--text-muted)',
                  }}
                >
                  <span className="uppercase mono mr-1">{finding.severity}</span>
                  {finding.message}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
