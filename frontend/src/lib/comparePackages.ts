/**
 * comparePackages — derive a PackageComparison from two real packages when the
 * fixture has no precomputed comparison for that pair. Uses only fields present
 * on `Package` (matrices, componentVersions, validation) — nothing invented.
 */
import type { Package, PackageComparison } from '@/types/domain';

export function findComparison(
  comparisons: PackageComparison[],
  aVersion: number,
  bVersion: number
): PackageComparison | undefined {
  return comparisons.find(
    (c) =>
      (c.a.version === aVersion && c.b.version === bVersion) ||
      (c.a.version === bVersion && c.b.version === aVersion)
  );
}

export function computeComparison(a: Package, b: Package, instrument = 'EURUSD'): PackageComparison {
  const ma = a.policy.matrices[instrument]?.cells ?? {};
  const mb = b.policy.matrices[instrument]?.cells ?? {};
  const keys = new Set([...Object.keys(ma), ...Object.keys(mb)]);

  const cellDiffs: PackageComparison['cellDiffs'] = [];
  for (const k of keys) {
    const ca = ma[k];
    const cb = mb[k];
    if (!ca || !cb) continue;
    const target = ca.target.rr !== cb.target.rr ? { from: ca.target.rr, to: cb.target.rr } : null;
    const risk = ca.risk.pct !== cb.risk.pct ? { from: ca.risk.pct, to: cb.risk.pct } : null;
    const eligibility =
      ca.eligibility.action !== cb.eligibility.action
        ? { from: ca.eligibility.action, to: cb.eligibility.action }
        : null;
    if (!target && !risk && !eligibility) continue;
    cellDiffs.push({
      scenarioKey: `${instrument}:${k}`,
      eligibility,
      target,
      risk,
      recommendationId: cb.provenanceRecommendationId ?? null,
      evidenceDelta: {
        expectancyR: { from: ca.evidence.expectancyR, to: cb.evidence.expectancyR },
        winRate: { from: ca.evidence.winRate, to: cb.evidence.winRate },
        badge: { from: ca.evidence.badge, to: cb.evidence.badge },
      },
    });
  }

  const componentVersionDiffs: PackageComparison['componentVersionDiffs'] = [];
  const comps = new Set([...Object.keys(a.componentVersions), ...Object.keys(b.componentVersions)]);
  for (const c of comps) {
    if (a.componentVersions[c] !== b.componentVersions[c]) {
      componentVersionDiffs.push({
        component: c,
        from: a.componentVersions[c] ?? '—',
        to: b.componentVersions[c] ?? '—',
      });
    }
  }

  const pdA = a.validation.portfolioDeltas;
  const pdB = b.validation.portfolioDeltas;
  return {
    comparisonId: `cmp_v${a.version}_v${b.version}`,
    a: { packageId: a.packageId, version: a.version, packageHash: a.packageHash },
    b: { packageId: b.packageId, version: b.version, packageHash: b.packageHash },
    cellDiffs,
    componentVersionDiffs,
    validationComparison: {
      a: { badge: a.validation.badge, portfolioDeltas: { ...pdA } },
      b: { badge: b.validation.badge, portfolioDeltas: { ...pdB } },
    },
    projectedImprovement: {
      netR: pdB.netR - pdA.netR,
      drawdown: pdB.drawdown - pdA.drawdown,
      stability: pdB.stability - pdA.stability,
      nDecided: b.validation.nDecided,
      inSampleCaveat: 'computed client-side from package matrices',
    },
    deploymentHistoryDelta: { a: { lanes: [], accounts: 0 }, b: { lanes: [], accounts: 0 } },
    generatedAt: new Date().toISOString(),
  };
}
