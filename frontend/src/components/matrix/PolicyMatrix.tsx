import { useMemo } from 'react';
import { usePolicyMatrix } from '@/hooks/useRepository';
import { useShellStore, type MatrixLens } from '@/store/shellStore';
import { cn } from '@/lib/utils';
import {
  eligibilityColor,
  badgeColor,
  marketStateGlyph,
  marketStateLabel,
  marketStateVar,
  sessionLabel,
} from '@/lib/utils';
import type { PolicyCell, MarketState } from '@/types/domain';
import { fmtR } from '@/lib/format';

/**
 * The Unified Policy Matrix — flagship component.
 * ONE 24×6 grid; a lens toggle swaps ONLY the cell renderer.
 * Frozen row/column headers; cell click → Inspector.
 * Per §G — never separate matrices; new lenses register a renderer.
 */

const MARKET_STATES: MarketState[] = [
  'BullExpand',
  'BullCompress',
  'BullChop',
  'BearExpand',
  'BearCompress',
  'BearChop',
];

interface CohortRow {
  key: string; // e.g. london:BOS:long
  session: string;
  structure: string;
  direction: string;
}

export function PolicyMatrix({ instrument }: { instrument: string }) {
  const matrix = usePolicyMatrix(instrument);
  const lens = useShellStore((s) => s.matrixLens);
  const setLens = useShellStore((s) => s.setMatrixLens);
  const openInspector = useShellStore((s) => s.openInspector);

  const cohortRows = useMemo<CohortRow[]>(() => {
    const rows: CohortRow[] = [];
    matrix.cohortAxis.sessions.forEach((session) => {
      matrix.cohortAxis.structures.forEach((structure) => {
        matrix.cohortAxis.directions.forEach((direction) => {
          const s = session.charAt(0).toLowerCase() + session.slice(1);
          const d = direction.toLowerCase();
          rows.push({
            key: `${s}:${structure}:${d}`,
            session,
            structure,
            direction,
          });
        });
      });
    });
    return rows;
  }, [matrix]);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Lens selector */}
      <div
        className="flex items-center gap-3 px-4 h-10 border-b shrink-0"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
      >
        <div className="text-[10px] uppercase tracking-widest text-text-muted font-medium">Lens</div>
        <div className="flex items-center gap-1" role="tablist" data-testid="matrix-lens-selector">
          {(['eligibility', 'targets', 'risk', 'recommendations', 'validation'] as MatrixLens[]).map((l) => (
            <button
              key={l}
              role="tab"
              aria-selected={lens === l}
              onClick={() => setLens(l)}
              data-testid={`matrix-lens-${l}`}
              className={cn(
                'h-7 px-3 rounded-sm text-xs font-medium transition-colors duration-fast',
                lens === l
                  ? 'bg-[color:var(--panel)] text-text border border-[color:var(--border)]'
                  : 'text-text-2 border border-transparent hover:text-text hover:bg-[color:var(--panel)]'
              )}
            >
              {l.charAt(0).toUpperCase() + l.slice(1)}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <div className="text-2xs text-text-muted mono tabular">
          {cohortRows.length} cohorts × {MARKET_STATES.length} states = {cohortRows.length * MARKET_STATES.length} cells
        </div>
      </div>

      {/* Matrix body */}
      <div className="flex-1 min-h-0 overflow-auto relative" data-testid="policy-matrix">
        <table
          className="w-full border-collapse text-sm tabular"
          style={{ borderColor: 'var(--border-subtle)' }}
        >
          <thead className="sticky top-0 z-20" style={{ background: 'var(--panel)' }}>
            <tr>
              <th
                className="sticky left-0 z-30 border-r border-b text-left px-3 py-2 text-2xs uppercase tracking-widest text-text-muted"
                style={{
                  background: 'var(--panel)',
                  borderColor: 'var(--border-subtle)',
                  minWidth: 200,
                }}
              >
                Cohort
              </th>
              {MARKET_STATES.map((ms) => (
                <th
                  key={ms}
                  className="border-b border-r text-center px-2 py-2"
                  style={{ borderColor: 'var(--border-subtle)', minWidth: 108 }}
                >
                  <div className="flex flex-col items-center gap-0.5">
                    <span
                      className="text-2xs mono font-semibold"
                      style={{ color: marketStateVar(ms) }}
                    >
                      {marketStateGlyph(ms)}
                    </span>
                    <span className="text-2xs uppercase tracking-wider" style={{ color: marketStateVar(ms) }}>
                      {marketStateLabel(ms)}
                    </span>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cohortRows.map((row, rowIdx) => {
              const groupBoundary = rowIdx > 0 && cohortRows[rowIdx - 1].session !== row.session;
              return (
                <tr key={row.key} className={cn(groupBoundary && 'border-t-2')}
                  style={{ borderTopColor: groupBoundary ? 'var(--border)' : undefined }}
                >
                  <th
                    scope="row"
                    className="sticky left-0 z-10 border-r border-b text-left px-3 py-2 text-xs font-medium"
                    style={{
                      background: 'var(--panel)',
                      borderColor: 'var(--border-subtle)',
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-text-2 tabular w-16 shrink-0 truncate">{sessionLabel(row.session)}</span>
                      <span className="text-text-muted">·</span>
                      <span className="text-text-2">{row.structure}</span>
                      <span className="text-text-muted">·</span>
                      <span
                        style={{
                          color:
                            row.direction.toLowerCase() === 'long'
                              ? 'var(--positive)'
                              : 'var(--negative)',
                        }}
                      >
                        {row.direction}
                      </span>
                    </div>
                  </th>
                  {MARKET_STATES.map((ms) => {
                    const cellKey = `${row.key}:${ms}`;
                    const cell = matrix.cells[cellKey];
                    return (
                      <td
                        key={ms}
                        onClick={() =>
                          openInspector({ kind: 'policyCell', instrument, cellKey })
                        }
                        className="border-r border-b p-0 cursor-pointer transition-colors duration-fast hover:bg-[color:var(--panel-2)]"
                        style={{ borderColor: 'var(--border-subtle)' }}
                        data-testid={`matrix-cell-${cellKey}`}
                      >
                        <MatrixCell cell={cell} lens={lens} />
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Legend */}
      <MatrixLegend lens={lens} />
    </div>
  );
}

/** Lens registry — a new lens registers a renderer, never a new screen (§G). */
function MatrixCell({ cell, lens }: { cell: PolicyCell | undefined; lens: MatrixLens }) {
  if (!cell) return <div className="h-10" />;
  switch (lens) {
    case 'eligibility':
      return <EligibilityCell cell={cell} />;
    case 'targets':
      return <TargetsCell cell={cell} />;
    case 'risk':
      return <RiskCell cell={cell} />;
    case 'recommendations':
      return <RecommendationsCell cell={cell} />;
    case 'validation':
      return <ValidationCell cell={cell} />;
  }
}

function EligibilityCell({ cell }: { cell: PolicyCell }) {
  const allowed = cell.eligibility.resolvedAllowed;
  const color = allowed ? eligibilityColor(cell.eligibility.action) : 'var(--negative)';
  const bg = allowed
    ? `color-mix(in srgb, ${color} 12%, transparent)`
    : 'color-mix(in srgb, var(--negative) 10%, transparent)';
  const glyph = allowed ? '●' : '○';
  const short =
    cell.eligibility.action === 'LABEL'
      ? 'Allow'
      : cell.eligibility.action === 'STATE_ONLY'
      ? 'Chop'
      : cell.eligibility.action === 'DIRECTION_AWARE'
      ? 'Trend'
      : 'Off';
  return (
    <div
      className="h-10 flex items-center justify-center gap-1.5"
      style={{ background: bg, color, opacity: allowed ? 1 : 0.85 }}
    >
      <span className="text-2xs mono font-semibold">{glyph}</span>
      <span className="text-xs font-medium">{short}</span>
    </div>
  );
}

function TargetsCell({ cell }: { cell: PolicyCell }) {
  if (!cell.eligibility.resolvedAllowed) {
    return (
      <div className="h-10 flex items-center justify-center text-text-muted text-2xs mono">
        —
      </div>
    );
  }
  const rr = cell.target.rr;
  const intensity = Math.min(1, rr / 4);
  return (
    <div
      className="h-10 flex flex-col items-center justify-center gap-0"
      style={{
        background: `color-mix(in srgb, var(--primary) ${Math.round(4 + intensity * 22)}%, transparent)`,
      }}
    >
      <span className="text-sm font-semibold text-text mono">{fmtR(rr, { sign: false })}</span>
      <span className="text-2xs text-text-muted mono">{cell.target.source === 'cell' ? 'cell' : 'base'}</span>
    </div>
  );
}

function RiskCell({ cell }: { cell: PolicyCell }) {
  const risk = cell.risk.pct;
  const isZero = risk === 0;
  const color = isZero ? 'var(--text-muted)' : risk >= 1.5 ? 'var(--warning)' : 'var(--text)';
  return (
    <div
      className="h-10 flex flex-col items-center justify-center gap-0"
      style={{
        background: isZero
          ? 'transparent'
          : `color-mix(in srgb, var(--warning) ${Math.round(risk * 15)}%, transparent)`,
      }}
    >
      <span className="text-sm font-semibold mono" style={{ color }}>{risk.toFixed(2)}%</span>
      <span className="text-2xs text-text-muted mono">{cell.risk.source === 'cell' ? 'cell' : 'base'}</span>
    </div>
  );
}

function ValidationCell({ cell }: { cell: PolicyCell }) {
  const bColor = badgeColor(cell.evidence.badge);
  const ev = cell.evidence.expectancyR;
  const evColor = ev > 0 ? 'var(--positive)' : ev < 0 ? 'var(--negative)' : 'var(--text-2)';
  return (
    <div className="h-10 flex flex-col items-center justify-center gap-0 px-1">
      <div className="flex items-center gap-1">
        <span
          className="text-[8px] mono font-bold uppercase px-1 rounded-sm"
          style={{ color: bColor, background: `color-mix(in srgb, ${bColor} 14%, transparent)` }}
        >
          {cell.evidence.badge.slice(0, 3)}
        </span>
        <span className="text-xs font-semibold mono" style={{ color: evColor }}>
          {ev > 0 ? '+' : ''}
          {ev.toFixed(2)}R
        </span>
      </div>
      <span className="text-2xs text-text-muted mono">n={cell.evidence.sampleSize}</span>
    </div>
  );
}

function RecommendationsCell({ cell }: { cell: PolicyCell }) {
  const st = cell.recommendationStatus;
  if (!st || st === 'none') {
    return <div className="h-10 flex items-center justify-center text-text-muted text-2xs mono">·</div>;
  }
  const color =
    st === 'deployed'
      ? 'var(--positive)'
      : st === 'promoted'
      ? 'var(--validated)'
      : st === 'validating'
      ? 'var(--warning)'
      : st === 'rejected'
      ? 'var(--negative)'
      : st === 'drafted'
      ? 'var(--draft)'
      : 'var(--recommendation)';
  return (
    <div
      className="h-10 flex items-center justify-center px-1"
      style={{ background: `color-mix(in srgb, ${color} 12%, transparent)` }}
    >
      <span className="text-2xs font-semibold uppercase tracking-wide text-center" style={{ color }}>
        {st}
      </span>
    </div>
  );
}

function MatrixLegend({ lens }: { lens: MatrixLens }) {
  return (
    <div
      className="flex items-center gap-4 h-8 px-4 border-t text-2xs text-text-muted shrink-0"
      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
    >
      <span className="uppercase tracking-widest">Legend</span>
      {lens === 'eligibility' && (
        <>
          <LegendItem color="var(--elig-follow-trend)" label="Follow trend" />
          <LegendItem color="var(--elig-always-allow)" label="Always allow" />
          <LegendItem color="var(--elig-block-chop)" label="Block chop" />
          <LegendItem color="var(--negative)" label="Not allowed" />
        </>
      )}
      {lens === 'targets' && (
        <>
          <LegendItem color="var(--primary)" label="RR value; intensity ∝ target" />
        </>
      )}
      {lens === 'risk' && (
        <>
          <LegendItem color="var(--warning)" label="Higher risk = warmer tint" />
        </>
      )}
      {lens === 'validation' && (
        <>
          <LegendItem color="var(--valid-native)" label="NATIVE" />
          <LegendItem color="var(--valid-rescore)" label="RESCORE" />
          <LegendItem color="var(--valid-base)" label="BASE" />
          <LegendItem color="var(--valid-insufficient)" label="INSUF." />
        </>
      )}
      {lens === 'recommendations' && (
        <>
          <LegendItem color="var(--recommendation)" label="Candidate" />
          <LegendItem color="var(--draft)" label="Drafted" />
          <LegendItem color="var(--validated)" label="Promoted" />
          <LegendItem color="var(--positive)" label="Deployed" />
        </>
      )}
      <span className="ml-auto mono">Click any cell → open Inspector</span>
    </div>
  );
}

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block rounded-sm" style={{ background: color, width: 10, height: 10 }} />
      <span>{label}</span>
    </span>
  );
}
