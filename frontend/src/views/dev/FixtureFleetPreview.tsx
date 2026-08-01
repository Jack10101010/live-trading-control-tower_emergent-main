import { useFixtureFleetPreview, useFixturePackagesPreview, useFixtureActivePackagePreview } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';

/**
 * M-FLEET-2 — the ONLY surface permitted to render fixture fleet records.
 *
 * The development fixture world still holds authored deployments, brokers and
 * accounts, and tests genuinely need them. What must never happen again is
 * those records appearing on an ordinary operator route, where an operator
 * reading a $100,000 balance and a +$412 daily P/L has no way to know they were
 * authored by hand.
 *
 * So the fixture lives here and nowhere else. This route is:
 *   - not linked from any navigation (reachable only by typing the URL);
 *   - the sole importer of `useFixtureFleetPreview`, enforced by a source guard;
 *   - unreachable in production, because M-ENV-1 never loads the fixture world
 *     and `/api/fleet` answers 501 there.
 *
 * A red border was not sufficient isolation. Physical separation is.
 */
export function FixtureFleetPreview() {
  const { deployments, brokers, accounts, available, provenanceDetail } = useFixtureFleetPreview();

  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-text tracking-tight">
          Fixture Fleet Preview
        </h1>
        <p
          className="text-xs mt-2 px-2 py-1 rounded border inline-block mono"
          style={{ borderColor: 'var(--mode-mock)', color: 'var(--mode-mock)' }}
          data-testid="fixture-preview-banner"
        >
          DEVELOPMENT FIXTURE — none of this is operational truth
        </p>
        <p className="text-xs text-text-muted mt-2 max-w-2xl">
          Authored demonstration records from the development fixture world, shown
          here so component development and tests have data to work against. No
          ordinary operator route renders these. Operational fleet data comes from{' '}
          <span className="mono">/api/operations/*</span> filtered to authoritative
          provenance.
        </p>
      </div>

      <FixturePackages />
      {!available ? (
        <EmptyState title="Fixture world not loaded" description={provenanceDetail} />
      ) : (
        <>
          <Panel provenance="fixture" title={`Fixture deployments (${deployments.length})`}>
            <ul className="text-xs space-y-1 mono">
              {deployments.map((d) => (
                <li key={d.deploymentId} data-testid={`fixture-deployment-${d.deploymentId}`}>
                  {d.pair} · {d.lane} · {d.status} · {d.executionMode}
                </li>
              ))}
            </ul>
          </Panel>
          <Panel provenance="fixture" title={`Fixture brokers (${brokers.length})`}>
            <ul className="text-xs space-y-1 mono">
              {brokers.map((b) => (
                <li key={b.brokerId}>{b.venue} · {b.status}</li>
              ))}
            </ul>
          </Panel>
          <Panel provenance="fixture" title={`Fixture accounts (${accounts.length})`}>
            <ul className="text-xs space-y-1 mono">
              {accounts.map((a) => (
                <li key={a.accountId}>
                  {a.type} · {a.baseCurrency} · balance {a.balance} · equity {a.equity}
                </li>
              ))}
            </ul>
          </Panel>
        </>
      )}
    </div>
  );
}

/**
 * M-PKG-1: the fixture's authored strategy packages. They used to drive the
 * Policy Engine, Versioning, StrategyPackages and System views — every version,
 * hash and promotion date an operator saw. No package registry exists.
 */
function FixturePackages() {
  const packages = useFixturePackagesPreview();
  const active = useFixtureActivePackagePreview();
  return (
    <Panel provenance="fixture" title={`Fixture packages (${packages.length})`}>
      <p className="text-2xs text-text-muted mb-2" data-testid="fixture-packages-note">
        Authored packages. No strategy-package registry exists; ordinary operator
        routes report this domain as unavailable.
      </p>
      <ul className="text-xs mono space-y-1" data-testid="fixture-packages-list">
        {packages.map((p) => (
          <li key={`${p.packageId}-v${p.version}`}>
            v{p.version} · {p.label} · {p.status}
            {active && p.packageHash === active.packageHash ? ' · (active in fixture)' : ''}
          </li>
        ))}
      </ul>
    </Panel>
  );
}
