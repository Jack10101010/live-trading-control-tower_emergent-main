import { AlertTriangle } from 'lucide-react';
import {
  CHART_MODES,
  type ChartContext,
  type TrustLevel,
  type WarnSeverity,
} from '@/lib/chartContext';

/**
 * ChartDataStatusStrip — a PROJECTION of ChartContext (it holds no logic of its own;
 * all interpretation lives in deriveChartContext). Mounted once by ChartWorkspace,
 * directly beneath the chart. Constant grammar so the operator recognises it from
 * peripheral vision: MODE · DATA · TRUST · LIVENESS · WARNINGS. Positions are fixed;
 * only values change. LIVENESS (is the pipe moving) is shown separately from TRUST
 * (is the data good) — they are never merged. Warnings appear only when present.
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
  ms == null ? '—' : new Date(ms).toISOString().slice(5, 10).replace('-', ' ').replace(/^0/, '');
const fmtAge = (s: number | null) =>
  s == null ? '' : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;

const Sep = () => <span aria-hidden style={{ color: 'var(--border)' }}>|</span>;

export function ChartDataStatusStrip({ context }: { context: ChartContext }) {
  const { mode, data, warnings } = context;
  const meta = CHART_MODES[mode];
  const ModeIcon = meta.icon;
  const trustColor = TRUST_COLOR[data.trust];

  return (
    <div
      data-testid="chart-status-strip"
      className="flex items-center gap-2 px-3 h-7 shrink-0 border-t overflow-x-auto text-2xs mono select-none"
      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
    >
      {/* MODE — coloured identity */}
      <span
        data-testid="chart-status-mode"
        className="inline-flex items-center gap-1.5 shrink-0"
        style={{ color: meta.color }}
        title={meta.blurb}
      >
        <ModeIcon size={13} aria-hidden />
        <span className="font-medium tracking-wide uppercase" style={{ letterSpacing: '.04em' }}>{meta.label}</span>
      </span>

      <Sep />

      {/* DATA — source + bar facts */}
      <span data-testid="chart-status-source" className="inline-flex items-center gap-2 shrink-0 text-text-2">
        <span className="text-text">{data.sourceLabel}</span>
        <span className="text-text-muted">
          {data.timeframe}
          <span className="mx-1" style={{ color: 'var(--border)' }}>·</span>
          {data.barCount} bars
          {data.lastBarMs != null && (
            <>
              <span className="mx-1" style={{ color: 'var(--border)' }}>·</span>
              <span title={`${fmtDay(data.firstBarMs)} ${fmtTime(data.firstBarMs)} → ${fmtDay(data.lastBarMs)} ${fmtTime(data.lastBarMs)} UTC`}>
                last {fmtTime(data.lastBarMs)}Z
                {data.lastBarAgeS != null && <span className="text-text-muted"> ({fmtAge(data.lastBarAgeS)})</span>}
              </span>
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
        title={data.cache.cached && data.cache.ageS != null ? `cache age ${Math.round(data.cache.ageS)}s` : undefined}
      >
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: trustColor }} />
        {data.trustLabel}
        {data.polygonStatus === 'DELAYED' && data.trust !== 'delayed' && <span className="text-text-muted"> · delayed</span>}
      </span>

      {/* LIVENESS — is the pipe moving? (separate from trust) */}
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
          {warnings.map((w) => (
            <span
              key={w.id}
              className="inline-flex items-center gap-1 px-1.5 rounded-sm whitespace-nowrap font-medium"
              style={{ color: SEVERITY_COLOR[w.severity], background: `color-mix(in srgb, ${SEVERITY_COLOR[w.severity]} 14%, transparent)` }}
            >
              <AlertTriangle size={11} aria-hidden />
              {w.text}
            </span>
          ))}
        </span>
      )}
    </div>
  );
}
