import { AlertTriangle } from 'lucide-react';
import {
  WORKSPACE_MODES,
  EXECUTION_MODES,
  type ChartContext,
  type TrustLevel,
  type WarnSeverity,
} from '@/lib/chartContext';

/**
 * ChartDataStatusStrip — a PROJECTION of ChartContext (no logic of its own; all
 * interpretation lives in deriveChartContext). Mounted once by ChartWorkspace,
 * directly beneath the chart.
 *
 * Constant grammar so the operator recognises it from peripheral vision:
 *   WORKSPACE · EXECUTION | DATA (source · tf · count · window policy) | TRUST | LIVENESS | WARNINGS
 * Positions fixed; only values change. WORKSPACE MODE and EXECUTION AUTHORITY are
 * separate badges — monitoring a delayed feed is never labelled "Live Trading".
 * LIVENESS (pipe moving) is separate from TRUST (data good). Warnings only when present.
 */

const TRUST_COLOR: Record<TrustLevel, string> = {
  trusted: 'var(--positive)',
  delayed: 'var(--warning)',
  cached: 'var(--primary)',
  fallback: 'var(--warning)',
  stale: 'var(--negative)',
  unavailable: 'var(--negative)',
};

const SEVERITY_COLOR: Record<WarnSeverity, string> = {
  info: 'var(--text-muted)',
  caution: 'var(--warning)',
  critical: 'var(--negative)',
};

const fmtTime = (ms: number | null) => (ms == null ? '—' : new Date(ms).toISOString().slice(11, 16));
const fmtDay = (ms: number | null) =>
  ms == null ? '—' : new Date(ms).toISOString().slice(5, 10).replace('-', ' ');
const fmtAge = (s: number | null) =>
  s == null ? '' : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;

const Sep = () => <span aria-hidden style={{ color: 'var(--border)' }}>|</span>;

export function ChartDataStatusStrip({ context }: { context: ChartContext }) {
  const { mode, execution, data, warnings } = context;
  const meta = WORKSPACE_MODES[mode];
  const ModeIcon = meta.icon;
  const exec = EXECUTION_MODES[execution.mode];
  const trustColor = TRUST_COLOR[data.trust];
  const w = data.window;

  // Detail projection (hover): requested vs returned, range, policy, cache, provider.
  const detail = [
    `Window policy: ${w.policyName} — ${w.requestedBars} bars${w.warmupBars ? ` + ${w.warmupBars} warm-up` : ''}`,
    `Requested ${w.totalRequested} · returned ${w.baseBars}${w.backfilledBars ? ` · +${w.backfilledBars} back-scrolled` : ''} · displayed ${w.displayedBars} (max ${w.maxBars})`,
    w.partial && w.partialReason ? `Partial: ${w.partialReason}` : null,
    data.firstBarMs != null ? `Range: ${fmtDay(data.firstBarMs)} ${fmtTime(data.firstBarMs)} → ${fmtDay(data.lastBarMs)} ${fmtTime(data.lastBarMs)} UTC` : null,
    data.polygonStatus ? `Provider status: ${data.polygonStatus}` : null,
    data.cache.cached ? `Cache: hit, age ${Math.round(data.cache.ageS ?? 0)}s` : 'Cache: fresh fetch',
    data.lastSuccessAtMs ? `Last successful sync: ${fmtTime(data.lastSuccessAtMs)}Z` : null,
    data.lastFailedAtMs ? `Last failed request: ${fmtTime(data.lastFailedAtMs)}Z` : null,
    data.providerNote ? `Provider note: ${data.providerNote}` : null,
    !w.allowBackfill ? 'Back-scroll disabled — window is exact' : null,
  ].filter(Boolean).join('\n');

  return (
    <div
      data-testid="chart-status-strip"
      className="flex items-center gap-2 px-3 h-7 shrink-0 border-t overflow-x-auto text-2xs mono select-none"
      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
    >
      {/* WORKSPACE MODE — what this surface is for */}
      <span
        data-testid="chart-status-mode"
        className="inline-flex items-center gap-1.5 shrink-0"
        style={{ color: meta.color }}
        title={meta.blurb}
      >
        <ModeIcon size={13} aria-hidden />
        <span className="font-medium uppercase" style={{ letterSpacing: '.04em' }}>{meta.label}</span>
      </span>

      {/* EXECUTION AUTHORITY — structurally separate from workspace mode */}
      <span
        data-testid="chart-status-execution"
        className="inline-flex items-center gap-1 shrink-0 px-1.5 rounded-sm"
        style={{ color: exec.color, background: `color-mix(in srgb, ${exec.color} 14%, transparent)` }}
        title={execution.note ?? undefined}
      >
        exec: {exec.label}
      </span>

      <Sep />

      {/* DATA — source · timeframe · displayed count · window policy · last bar */}
      <span data-testid="chart-status-source" className="inline-flex items-center gap-2 shrink-0 text-text-2" title={detail}>
        <span className="text-text">{data.sourceLabel}</span>
        <span className="text-text-muted">
          {data.timeframe}
          <span className="mx-1" style={{ color: 'var(--border)' }}>·</span>
          <span className="text-text-2">{w.displayedBars} bars</span>
          <span className="mx-1" style={{ color: 'var(--border)' }}>·</span>
          {w.policyName}
          {w.warmupBars > 0 && ` +${w.warmupBars} warm-up`}
          {w.backfilledBars > 0 && ` +${w.backfilledBars} scrolled`}
          {data.lastBarMs != null && (
            <>
              <span className="mx-1" style={{ color: 'var(--border)' }}>·</span>
              last {fmtTime(data.lastBarMs)}Z
              {data.lastBarAgeS != null && <span className="text-text-muted"> ({fmtAge(data.lastBarAgeS)})</span>}
            </>
          )}
        </span>
      </span>

      <Sep />

      {/* TRUST — is the data good? */}
      <span
        data-testid="chart-status-trust"
        className="inline-flex items-center gap-1.5 px-1.5 rounded-sm shrink-0 font-medium"
        style={{ color: trustColor, background: `color-mix(in srgb, ${trustColor} 14%, transparent)` }}
        title={detail}
      >
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: trustColor }} />
        {data.trustLabel}
      </span>

      {/* LIVENESS — is the pipe moving? (never merged with trust) */}
      <span data-testid="chart-status-liveness" className="inline-flex items-center gap-1.5 shrink-0 text-text-2">
        <span
          className={`w-1.5 h-1.5 rounded-full ${data.updating ? 'animate-pulse' : ''}`}
          style={{ background: data.updating ? 'var(--positive)' : data.liveness === 'idle' ? 'var(--warning)' : 'var(--text-muted)' }}
        />
        {data.livenessLabel}
      </span>

      {/* WARNINGS — only when present; silence = healthy */}
      {warnings.length > 0 && (
        <span data-testid="chart-status-warnings" className="inline-flex items-center gap-1.5 ml-auto shrink-0">
          {warnings.map((wn) => (
            <span
              key={wn.id}
              className="inline-flex items-center gap-1 px-1.5 rounded-sm whitespace-nowrap font-medium"
              style={{ color: SEVERITY_COLOR[wn.severity], background: `color-mix(in srgb, ${SEVERITY_COLOR[wn.severity]} 14%, transparent)` }}
            >
              <AlertTriangle size={11} aria-hidden />
              {wn.text}
            </span>
          ))}
        </span>
      )}
    </div>
  );
}
