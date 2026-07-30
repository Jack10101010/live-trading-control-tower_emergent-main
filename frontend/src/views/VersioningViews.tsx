import { GitBranch, GitCompare, ArrowRight, Rocket, Undo2, Eye } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useMemo } from 'react';
import { usePackages, usePackageComparisons, useDeploymentsForPackage } from '@/hooks/useRepository';
import { useCommand } from '@/hooks/useCommand';
import { WorkspacePage } from '@/components/structures/WorkspacePage';
import { FeatureGate } from '@/components/FeatureGate';
import { Panel, DiffView, EmptyState } from '@/components/structures/Panel';
import { DataTable, type Column } from '@/components/structures/DataTable';
import {
  Badge,
  PackageVersionChip,
  ValidationBadgeChip,
  TimestampUTC,
  RValue,
  CohortChip,
  MetricStat,
} from '@/components/primitives';
import { Button } from '@/components/primitives/Button';
import { fmtHash } from '@/lib/format';
import { parseScenarioKey } from '@/lib/utils';
import { findComparison, computeComparison } from '@/lib/comparePackages';
import type { Package } from '@/types/domain';

/* ========================================================================== */
/*  Version History                                                            */
/* ========================================================================== */

export function VersionHistoryView() {
  const packages = usePackages();
  const navigate = useNavigate();
  const dispatch = useCommand();
  const active = packages.find((p) => p.status === 'active');

  const rows = packages.slice().sort((a, b) => b.version - a.version);

  const columns: Column<Package>[] = [
    { key: 'version', header: 'Version', cell: (p) => <PackageVersionChip version={p.version} hash={p.packageHash} />, width: 150 },
    { key: 'status', header: 'Status', cell: (p) => <Badge variant="status">{p.status}</Badge> },
    { key: 'validation', header: 'Validation', cell: (p) => <ValidationBadgeChip badge={p.validation.badge} /> },
    { key: 'created', header: 'Created', cell: (p) => <TimestampUTC iso={p.createdAt} /> },
    { key: 'promoted', header: 'Promoted', cell: (p) => (p.promotedAt ? <TimestampUTC iso={p.promotedAt} /> : <span className="text-text-muted">—</span>) },
    { key: 'superseded', header: 'Superseded', align: 'right', cell: (p) => (p.supersededBy ? <span className="mono">v{p.supersededBy}</span> : <span className="text-text-muted">—</span>) },
    { key: 'deps', header: 'Deps', align: 'right', mono: true, cell: (p) => <DeploymentCount hash={p.packageHash} /> },
    { key: 'hash', header: 'Hash', mono: true, cell: (p) => <span className="text-text-muted">{fmtHash(p.packageHash)}</span> },
    {
      key: 'actions',
      header: '',
      align: 'right',
      width: 220,
      cell: (p) => (
        <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
          <Button variant="ghost" size="sm" icon={<Eye size={11} />} onClick={() => navigate(`/package-comparison?a=${p.version}&b=${active?.version ?? p.version}`)}>
            Inspect
          </Button>
          <Button variant="ghost" size="sm" icon={<GitCompare size={11} />} onClick={() => navigate(`/package-comparison?a=${active?.version ?? p.version}&b=${p.version}`)}>
            Compare
          </Button>
          {p.status !== 'active' ? (
            <Button variant="outline" size="sm" icon={<Rocket size={11} />} onClick={() => dispatch({ name: 'DeployPackage', payload: { packageVersion: p.version } })}>
              Promote
            </Button>
          ) : (
            <Button variant="ghost" size="sm" icon={<Undo2 size={11} />} onClick={() => dispatch({ name: 'RollbackPackage', payload: { toVersion: (p.parentVersion ?? p.version) } })}>
              Rollback
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <FeatureGate flag="versionHistory">
      <WorkspacePage title="Version History" subtitle="Complete lineage of every Strategy Package — status · validation · deployments · hash">
        <Panel provenance="fixture" title={<span className="flex items-center gap-2"><GitBranch size={13} /> Packages</span>} dense>
          <DataTable columns={columns} data={rows} rowKey={(p) => `${p.packageId}-${p.version}`} />
        </Panel>
        <div className="text-2xs text-text-muted mt-3">
          Inspect / Compare open the diff view. Promote &amp; Rollback are commands (mock) routed through the single ConfirmDialog. No editing here — packages are immutable.
        </div>
      </WorkspacePage>
    </FeatureGate>
  );
}

function DeploymentCount({ hash }: { hash: string }) {
  const deps = useDeploymentsForPackage(hash);
  return <span>{deps.length}</span>;
}

/* ========================================================================== */
/*  Package Comparison                                                         */
/* ========================================================================== */

export function PackageComparisonView() {
  const packages = usePackages();
  const comparisons = usePackageComparisons();
  const [params, setParams] = useSearchParams();

  const sorted = packages.slice().sort((a, b) => b.version - a.version);
  const activeV = packages.find((p) => p.status === 'active')?.version ?? sorted[0]?.version;
  const priorV = packages.find((p) => p.status !== 'active')?.version ?? sorted[1]?.version ?? activeV;

  const aVersion = Number(params.get('a')) || priorV;
  const bVersion = Number(params.get('b')) || activeV;

  const pkgA = packages.find((p) => p.version === aVersion);
  const pkgB = packages.find((p) => p.version === bVersion);

  const cmp = useMemo(() => {
    if (!pkgA || !pkgB) return undefined;
    return findComparison(comparisons, aVersion, bVersion) ?? computeComparison(pkgA, pkgB);
  }, [comparisons, pkgA, pkgB, aVersion, bVersion]);

  const setVersion = (side: 'a' | 'b', v: number) => {
    const next = new URLSearchParams(params);
    next.set(side, String(v));
    next.set(side === 'a' ? 'b' : 'a', String(side === 'a' ? bVersion : aVersion));
    setParams(next, { replace: true });
  };

  return (
    <FeatureGate flag="packageComparison">
      <WorkspacePage title="Package Comparison" subtitle="Only changed values are shown — cells · targets · eligibility · risk · validation · component versions">
        {/* Selectors */}
        <div className="flex items-center gap-3 mb-4">
          <VersionSelect label="Base (A)" value={aVersion} options={sorted} onChange={(v) => setVersion('a', v)} />
          <ArrowRight size={16} className="text-text-muted mt-4" />
          <VersionSelect label="Compare (B)" value={bVersion} options={sorted} onChange={(v) => setVersion('b', v)} />
        </div>

        {!cmp || aVersion === bVersion ? (
          <EmptyState title="Select two different packages" description="Choose a base and a comparison version to see the diff." icon={<GitCompare size={18} />} />
        ) : (
          <div className="space-y-4">
            {/* Projected improvement */}
            <Panel provenance="fixture" title="Projected impact (B vs A)" dense>
              <div className="grid grid-cols-4 gap-3">
                <MetricStat label="Δ net R" value={<RValue value={cmp.projectedImprovement.netR} />} mono />
                <MetricStat label="Δ drawdown" value={cmp.projectedImprovement.drawdown.toFixed(2)} mono />
                <MetricStat label="Δ stability" value={cmp.projectedImprovement.stability.toFixed(2)} mono />
                <MetricStat label="n decided" value={cmp.projectedImprovement.nDecided} mono />
              </div>
              <div className="text-2xs text-text-muted mt-2 italic">{cmp.projectedImprovement.inSampleCaveat}</div>
            </Panel>

            {/* Validation comparison */}
            <Panel provenance="fixture" title="Validation" dense>
              <div className="flex items-center gap-3">
                <span className="text-2xs text-text-muted">A</span>
                <ValidationBadgeChip badge={cmp.validationComparison.a.badge} />
                <ArrowRight size={12} className="text-text-muted" />
                <span className="text-2xs text-text-muted">B</span>
                <ValidationBadgeChip badge={cmp.validationComparison.b.badge} />
              </div>
            </Panel>

            {/* Component version diffs */}
            <Panel provenance="fixture" title={<>Component versions <span className="text-text-muted mono ml-1">({cmp.componentVersionDiffs.length} changed)</span></>} dense>
              {cmp.componentVersionDiffs.length === 0 ? (
                <div className="text-xs text-text-muted italic">No component version changes</div>
              ) : (
                <ul className="space-y-1.5">
                  {cmp.componentVersionDiffs.map((c) => (
                    <li key={c.component} className="flex items-center gap-3">
                      <span className="text-2xs uppercase tracking-widest text-text-muted w-40 shrink-0">{c.component}</span>
                      <DiffView before={c.from} after={c.to} />
                    </li>
                  ))}
                </ul>
              )}
            </Panel>

            {/* Cell diffs */}
            <Panel provenance="fixture" title={<>Policy cells changed <span className="text-text-muted mono ml-1">({cmp.cellDiffs.length})</span></>} dense>
              {cmp.cellDiffs.length === 0 ? (
                <div className="text-xs text-text-muted italic">No cell-level changes between these versions</div>
              ) : (
                <DataTable
                  columns={cellDiffColumns}
                  data={cmp.cellDiffs}
                  rowKey={(d) => d.scenarioKey}
                />
              )}
            </Panel>
          </div>
        )}
      </WorkspacePage>
    </FeatureGate>
  );
}

type CellDiff = ReturnType<typeof computeComparison>['cellDiffs'][number];

const cellDiffColumns: Column<CellDiff>[] = [
  {
    key: 'cohort',
    header: 'Cohort × State',
    cell: (d) => {
      const p = parseScenarioKey(d.scenarioKey);
      return (
        <div className="flex items-center gap-1.5">
          <CohortChip session={p.session} structure={p.structure} direction={p.direction} />
          <span className="text-2xs text-text-muted mono">{p.marketState}</span>
        </div>
      );
    },
  },
  {
    key: 'eligibility',
    header: 'Eligibility',
    cell: (d) => (d.eligibility ? <DiffView before={d.eligibility.from} after={d.eligibility.to} /> : <span className="text-text-muted">—</span>),
  },
  {
    key: 'target',
    header: 'Target',
    align: 'right',
    cell: (d) => (d.target ? <DiffView before={`${d.target.from}R`} after={`${d.target.to}R`} /> : <span className="text-text-muted">—</span>),
  },
  {
    key: 'risk',
    header: 'Risk',
    align: 'right',
    cell: (d) => (d.risk ? <DiffView before={`${d.risk.from}%`} after={`${d.risk.to}%`} /> : <span className="text-text-muted">—</span>),
  },
  {
    key: 'ev',
    header: 'Δ Expectancy',
    align: 'right',
    mono: true,
    cell: (d) => (
      <span className="text-2xs">
        <span className="text-text-muted">{d.evidenceDelta.expectancyR.from.toFixed(2)}</span>
        <span className="text-text-muted"> → </span>
        <span className="text-text">{d.evidenceDelta.expectancyR.to.toFixed(2)}</span>
      </span>
    ),
  },
  {
    key: 'badge',
    header: 'Validation',
    cell: (d) =>
      d.evidenceDelta.badge.from === d.evidenceDelta.badge.to ? (
        <ValidationBadgeChip badge={d.evidenceDelta.badge.to} />
      ) : (
        <DiffView before={d.evidenceDelta.badge.from} after={d.evidenceDelta.badge.to} />
      ),
  },
];

function VersionSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: number;
  options: Package[];
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-2xs uppercase tracking-widest text-text-muted">{label}</span>
      <select
        className="h-8 rounded-md border px-2 text-sm text-text bg-[color:var(--panel-2)] outline-none focus-visible:border-[color:var(--focus)]"
        style={{ borderColor: 'var(--border)' }}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      >
        {options.map((p) => (
          <option key={p.version} value={p.version}>
            Package v{p.version} · {p.status} · {p.label}
          </option>
        ))}
      </select>
    </label>
  );
}
