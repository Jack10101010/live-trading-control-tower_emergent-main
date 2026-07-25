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

      {data?.cors && (
        /* UI-10 — the browser boundary. Distinct from the transport section below:
           CORS is live and enforced TODAY, whereas remote transport is NOT ACTIVE.
           Counts and classifications only; no origin string is ever sent or shown. */
        <div
          className="mt-1 pt-1 border-t border-[color:var(--border)] text-2xs"
          data-testid="cors-policy"
        >
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-text-2">Browser origin policy (CORS)</span>
            <span className="mono" data-testid="cors-source">{data.cors.source}</span>
          </div>
          <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 mt-0.5 text-text-muted">
            <dt>Scope</dt>
            <dd className="mono" data-testid="cors-scope">
              {data.cors.localOnly ? 'local only' : 'externally configured'}
            </dd>
            <dt>Trusted origins</dt>
            <dd className="mono" data-testid="cors-origin-count">{data.cors.originCount}</dd>
            <dt>Wildcard</dt>
            <dd className="mono" data-testid="cors-wildcard">
              {data.cors.wildcardEnabled ? 'ENABLED' : 'disabled'}
            </dd>
            <dt>Credentials</dt>
            <dd className="mono" data-testid="cors-credentials">
              {data.cors.credentialsEnabled ? 'enabled' : 'disabled'}
            </dd>
            <dt>Methods</dt>
            <dd className="mono">{data.cors.allowedMethods.join(' ')}</dd>
            <dt>Headers</dt>
            <dd className="mono">{data.cors.allowedHeaders.join(' ')}</dd>
            <dt>Validation</dt>
            <dd className="mono" data-testid="cors-validation">
              {data.cors.valid ? 'valid' : 'issues found'}
            </dd>
          </dl>
          {data.cors.source === 'invalid_fallback' && (
            <div className="mt-1" style={{ color: 'var(--negative)' }}>
              CORS_ORIGINS was set but no entry was usable. The safe local-only
              default is in force — access was not broadened.
            </div>
          )}
          {data.cors.issueCodes.length > 0 && (
            <ul className="mt-0.5" data-testid="cors-issues">
              {data.cors.issueCodes.map((code) => (
                <li key={code} className="mono text-text-muted">{code}</li>
              ))}
            </ul>
          )}
          <p className="mt-1 text-text-muted">
            CORS restricts browsers only. It is not authentication and does not
            protect against direct API clients.
          </p>
        </div>
      )}

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
