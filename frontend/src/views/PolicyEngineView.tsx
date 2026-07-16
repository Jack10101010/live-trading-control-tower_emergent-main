import { useOutletContext } from 'react-router-dom';
import { PolicyMatrix } from '@/components/matrix/PolicyMatrix';
import {
  useRecommendations,
  useDrafts,
  useActivePackage,
  usePolicyMatrix,
  usePackages,
} from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import { Panel, DiffView } from '@/components/structures/Panel';
import {
  Badge,
  KeyValueGrid,
  PackageVersionChip,
  RValue,
  SampleSize,
  TimestampUTC,
  ValidationBadgeChip,
  ConfidenceMeter,
} from '@/components/primitives';
import { Button } from '@/components/primitives/Button';
import { Pencil, CircleCheck, Rocket, ArrowUpCircle, Lock, GitCommit } from 'lucide-react';
import { fmtProbability } from '@/lib/format';
import { useCommand } from '@/hooks/useCommand';
import { FeatureGate } from '@/components/FeatureGate';

/**
 * PolicyEngineView — flagship matrix + rail with:
 *   · Active Package summary
 *   · Recommendations (P1 candidates)
 *   · Drafts (workspace-in-progress, read-only in v1)
 *   · Native Validation (last completed run — read-only)
 *   · Promotion Ladder (Draft → Validating → Validated → Promoted → Live)
 *   · Live Overrides (read-only stub)
 *
 * Edit / Validate / Promote / Deploy buttons render but are disabled with
 * an explanatory tooltip. Mutation flows are deferred to a later phase.
 */

export function PolicyEngineView() {
  const { pair } = useOutletContext<{ pair: string }>();
  const recs = useRecommendations();
  const drafts = useDrafts();
  const pkg = useActivePackage();
  const packages = usePackages();
  const openInspector = useShellStore((s) => s.openInspector);
  const matrix = usePolicyMatrix(pair);
  const dispatch = useCommand();

  const cellCount = Object.keys(matrix.cells).length;
  const allowedCount = Object.values(matrix.cells).filter((c) => c.eligibility.resolvedAllowed).length;
  const nativeCount = Object.values(matrix.cells).filter((c) => c.evidence.badge === 'NATIVE').length;

  return (
    <div className="grid grid-cols-[1fr_360px] h-full min-h-0">
      {/* Matrix */}
      <div className="flex flex-col min-h-0 overflow-hidden">
        <div
          className="flex items-center gap-4 px-4 h-10 border-b shrink-0"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          <h2 className="text-sm font-semibold text-text">Unified Policy Matrix</h2>
          <span className="text-2xs text-text-muted mono">{pair}</span>
          <div className="ml-auto flex items-center gap-3 text-2xs text-text-muted mono">
            <span>{cellCount} cells</span>
            <span>·</span>
            <span>{allowedCount} allowed</span>
            <span>·</span>
            <span>{nativeCount} NATIVE</span>
          </div>
        </div>
        <div className="flex-1 min-h-0">
          <PolicyMatrix instrument={pair} />
        </div>
      </div>

      {/* Rail */}
      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <div className="p-4 space-y-4">
          <ActivePackageCard
            pkg={pkg}
            onEdit={() => dispatch({ name: 'CreateDraft', payload: { baseVersion: pkg.version } })}
          />

          <FeatureGate flag="aiRecommendations" fallback={null}>
            <RecommendationsCard recs={recs} onOpen={(id) => openInspector({ kind: 'recommendation', recommendationId: id })} />
          </FeatureGate>

          <DraftsCard
            drafts={drafts}
            onValidate={(id) => dispatch({ name: 'RunNativeValidation', payload: { target: id } })}
            onPromote={(id) => dispatch({ name: 'PromoteDraft', payload: { draftId: id } })}
          />

          <NativeValidationCard pkg={pkg} />

          <PromotionLadder
            pkg={pkg}
            packages={packages}
            onDeploy={() => dispatch({ name: 'DeployPackage', payload: { packageVersion: pkg.version } })}
          />

          <LiveOverridesCard />
        </div>
      </aside>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Cards                                                                     */
/* -------------------------------------------------------------------------- */

function ActivePackageCard({ pkg, onEdit }: { pkg: ReturnType<typeof useActivePackage>; onEdit: () => void }) {
  return (
    <Panel
      title="Active Package"
      dense
      actions={
        <ActionButton
          icon={<Pencil size={11} />}
          label="Edit"
          reason="Package edits mutate the atomic version chain; deferred until backend wiring."
          onClick={onEdit}
          testid="pkg-edit-btn"
        />
      }
    >
      <KeyValueGrid
        items={[
          { label: 'Label', value: pkg.label },
          { label: 'Version', value: `v${pkg.version}`, mono: true },
          { label: 'Stage', value: pkg.stage },
          { label: 'Validation', value: <Badge variant="validation">{pkg.validation.badge}</Badge> },
          { label: 'Δ netR', value: <RValue value={pkg.validation.portfolioDeltas.netR} />, mono: true },
          { label: 'nDecided', value: pkg.validation.nDecided, mono: true },
          { label: 'Promoted', value: pkg.promotedAt ? <TimestampUTC iso={pkg.promotedAt} /> : '—' },
        ]}
      />
    </Panel>
  );
}

function RecommendationsCard({
  recs,
  onOpen,
}: {
  recs: ReturnType<typeof useRecommendations>;
  onOpen: (id: string) => void;
}) {
  return (
    <Panel
      title={
        <>
          Recommendations <span className="text-text-muted mono ml-1">({recs.length})</span>
        </>
      }
      dense
    >
      {recs.length === 0 ? (
        <div className="text-xs text-text-muted italic">No open recommendations</div>
      ) : (
        <ul className="space-y-2">
          {recs.map((r) => (
            <li
              key={r.recommendationId}
              className="rounded-md border p-2 cursor-pointer hover:bg-[color:var(--panel-2)] transition-colors"
              style={{ borderColor: 'var(--border-subtle)' }}
              onClick={() => onOpen(r.recommendationId)}
              data-testid={`recommendation-${r.recommendationId.slice(-8)}`}
            >
              <div className="flex items-center gap-2 mb-1">
                <Badge variant="recommendation" size="sm">
                  {r.status}
                </Badge>
                <ValidationBadgeChip badge={r.evidence.nativeValidation.badge} />
              </div>
              <div className="text-xs text-text mb-1 truncate mono">{r.scenarioKey}</div>
              <DiffView label={r.proposedChange.field} before={String(r.proposedChange.from)} after={String(r.proposedChange.to)} />
              <div className="mt-1.5 flex items-center gap-2 text-2xs text-text-muted">
                <SampleSize n={r.evidence.sampleSize} />
                <span>·</span>
                <span className="mono">{fmtProbability(r.evidence.supportingStats.pBetter ?? 0)}</span>
                <ConfidenceMeter value={r.evidence.confidence} width={40} showValue={false} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function DraftsCard({
  drafts,
  onValidate,
  onPromote,
}: {
  drafts: ReturnType<typeof useDrafts>;
  onValidate: (draftId: string) => void;
  onPromote: (draftId: string) => void;
}) {
  return (
    <Panel
      title={
        <>
          Drafts <span className="text-text-muted mono ml-1">({drafts.length})</span>
        </>
      }
      dense
    >
      {drafts.length === 0 ? (
        <div className="text-xs text-text-muted italic">No open drafts</div>
      ) : (
        <ul className="space-y-2">
          {drafts.map((d) => (
            <li
              key={d.draftId}
              className="rounded-md border p-2 space-y-2"
              style={{ borderColor: 'var(--border-subtle)' }}
              data-testid={`draft-${d.draftId.slice(-8)}`}
            >
              <div className="flex items-center gap-2">
                <Badge variant="draft" size="sm">
                  {d.status}
                </Badge>
                <span className="text-2xs text-text-muted">base v{d.baseVersion}</span>
                <span className="ml-auto text-2xs text-text-muted">
                  <TimestampUTC iso={d.createdAt} />
                </span>
              </div>
              <ul className="space-y-1">
                {d.changes.map((c, i) => (
                  <li key={i} className="text-2xs mono text-text-2 truncate" title={c.policyCellKey}>
                    <span className="text-text-muted">·</span> {c.policyCellKey.split(':').slice(-3).join(':')}
                  </li>
                ))}
              </ul>
              <div className="flex items-center gap-1.5 pt-1.5 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                <ActionButton
                  icon={<CircleCheck size={11} />}
                  label="Validate"
                  reason="Run native validation against pinned research data (mock)."
                  onClick={() => onValidate(d.draftId)}
                  testid={`draft-validate-${d.draftId.slice(-8)}`}
                />
                <ActionButton
                  icon={<ArrowUpCircle size={11} />}
                  label="Promote"
                  reason="Promote this draft to a new package version (atomic swap, mock)."
                  onClick={() => onPromote(d.draftId)}
                  testid={`draft-promote-${d.draftId.slice(-8)}`}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function NativeValidationCard({ pkg }: { pkg: ReturnType<typeof useActivePackage> }) {
  const v = pkg.validation;
  return (
    <Panel title="Native Validation (latest)" dense>
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <ValidationBadgeChip badge={v.badge} />
          <span className="text-2xs text-text-muted mono">v{pkg.version} → active</span>
        </div>
        <KeyValueGrid
          items={[
            { label: 'nDecided', value: v.nDecided, mono: true },
            { label: 'Δ netR', value: <RValue value={v.portfolioDeltas.netR} />, mono: true },
            { label: 'Δ drawdown', value: `${v.portfolioDeltas.drawdown.toFixed(2)}`, mono: true },
            { label: 'Stability', value: v.portfolioDeltas.stability.toFixed(2), mono: true },
            { label: 'Validation ID', value: v.validationId ?? '—', mono: true },
          ]}
        />
        <div
          className="text-2xs text-text-muted rounded-md p-2 border"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        >
          A NATIVE badge means the cohort was tested with sample size ≥ threshold on pinned research data. RESCORE / BASE / INSUFFICIENT / NOT_TESTED are the four fallback tiers.
        </div>
      </div>
    </Panel>
  );
}

function PromotionLadder({
  pkg,
  packages,
  onDeploy,
}: {
  pkg: ReturnType<typeof useActivePackage>;
  packages: ReturnType<typeof usePackages>;
  onDeploy: () => void;
}) {
  const stages = [
    { key: 'draft', label: 'Draft' },
    { key: 'validating', label: 'Validating' },
    { key: 'validated', label: 'Validated' },
    { key: 'promoted', label: 'Promoted' },
    { key: 'live', label: 'Live' },
  ] as const;

  // Determine current package position
  const currentStage: (typeof stages)[number]['key'] =
    pkg.status === 'active' ? 'live' : pkg.promotedAt ? 'promoted' : pkg.validation.badge === 'NATIVE' ? 'validated' : 'draft';
  const currentIdx = stages.findIndex((s) => s.key === currentStage);

  return (
    <Panel title="Promotion Ladder" dense>
      <div className="space-y-3">
        {/* Ladder rail */}
        <div className="flex items-center justify-between">
          {stages.map((s, i) => {
            const active = i <= currentIdx;
            return (
              <div key={s.key} className="flex-1 flex items-center min-w-0">
                <div className="flex flex-col items-center gap-1 min-w-0">
                  <span
                    className="w-3 h-3 rounded-full border"
                    style={{
                      background: active ? 'var(--primary)' : 'var(--panel-3)',
                      borderColor: active ? 'var(--primary)' : 'var(--border)',
                    }}
                  />
                  <span className={active ? 'text-2xs text-text' : 'text-2xs text-text-muted'}>{s.label}</span>
                </div>
                {i < stages.length - 1 && (
                  <span
                    className="h-px flex-1 mx-1 mb-4"
                    style={{ background: i < currentIdx ? 'var(--primary)' : 'var(--border-subtle)' }}
                  />
                )}
              </div>
            );
          })}
        </div>

        {/* Recent history */}
        <div className="space-y-1 pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="text-2xs uppercase tracking-widest text-text-muted mb-1">Recent versions</div>
          {packages
            .slice()
            .sort((a, b) => b.version - a.version)
            .slice(0, 4)
            .map((p) => (
              <div key={`${p.packageId}-${p.version}`} className="flex items-center gap-2 text-2xs">
                <GitCommit size={10} className="text-text-muted" />
                <PackageVersionChip version={p.version} />
                <Badge variant="status" size="sm">
                  {p.status}
                </Badge>
                <span className="ml-auto text-text-muted mono">
                  {p.promotedAt ? <TimestampUTC iso={p.promotedAt} /> : '—'}
                </span>
              </div>
            ))}
        </div>

        <ActionButton
          icon={<Rocket size={11} />}
          label="Deploy new version"
          reason="Deployments require broker adapter reconciliation. Not yet wired."
          onClick={onDeploy}
          fullWidth
          testid="deploy-btn"
        />
      </div>
    </Panel>
  );
}

function LiveOverridesCard() {
  return (
    <Panel
      title={
        <span className="flex items-center gap-1.5">
          Live Overrides <Lock size={10} className="text-text-muted" />
        </span>
      }
      dense
    >
      <div className="text-xs text-text-muted italic">
        No active overrides. All live changes flow through drafts → validation → atomic version swap. Reason-gated overrides land in Phase 2.
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/*  ActionButton — enabled command affordance. Dispatches via useCommand;       */
/*  confirmation + execution are handled centrally (mock backend).             */
/* -------------------------------------------------------------------------- */

function ActionButton({
  icon,
  label,
  reason,
  onClick,
  fullWidth,
  testid,
}: {
  icon: React.ReactNode;
  label: string;
  reason: string;
  onClick: () => void;
  fullWidth?: boolean;
  testid?: string;
}) {
  return (
    <button
      onClick={onClick}
      title={`${label} — ${reason}`}
      data-testid={testid}
      className={
        'inline-flex items-center gap-1.5 h-6 px-2 rounded-sm text-2xs font-medium border transition-colors duration-fast hover:bg-[color:var(--panel-3)] ' +
        (fullWidth ? 'w-full justify-center' : '')
      }
      style={{
        borderColor: 'var(--border)',
        background: 'var(--panel-2)',
        color: 'var(--text)',
      }}
    >
      {icon}
      {label}
    </button>
  );
}
