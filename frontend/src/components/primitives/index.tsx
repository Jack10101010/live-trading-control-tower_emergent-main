import { cn } from '@/lib/utils';
import { fmtR } from '@/lib/format';
import type { ReactNode } from 'react';
import { Badge, type BadgeVariant } from './Badge';

export { Badge } from './Badge';
export type { BadgeVariant } from './Badge';
export { Button, IconButton } from './Button';
import { badgeColor, marketStateGlyph, marketStateLabel, marketStateVar, eligibilityColor, eligibilityLabel, laneColor, laneLabel } from '@/lib/utils';
import { CircleDot, CircleAlert, CircleSlash, CircleCheck } from 'lucide-react';
import { PROVENANCE_HINT, PROVENANCE_LABEL, PROVENANCE_TONE, type DataProvenance } from '@/types/provenance';

/* -------------------------------------------------------------------------- */
/*  HealthDot — one indicator (dot + label + tooltip) for every domain        */
/* -------------------------------------------------------------------------- */

interface HealthDotProps {
  state: 'ok' | 'warn' | 'critical' | 'muted' | 'live';
  size?: 'sm' | 'md';
  pulse?: boolean;
  label?: string;
  title?: string;
}

export function HealthDot({ state, size = 'sm', pulse, label, title }: HealthDotProps) {
  const color =
    state === 'ok'
      ? 'var(--healthy)'
      : state === 'warn'
      ? 'var(--warning)'
      : state === 'critical'
      ? 'var(--negative)'
      : state === 'live'
      ? 'var(--live)'
      : 'var(--paused)';
  const dim = size === 'sm' ? 8 : 10;
  return (
    <span className="inline-flex items-center gap-1.5" title={title ?? label}>
      <span
        className={cn('inline-block rounded-full', pulse && 'ct-pulse-dot')}
        style={{
          width: dim,
          height: dim,
          background: color,
          boxShadow: `0 0 0 2px ${color}22`,
        }}
      />
      {label && <span className="text-xs text-text-2">{label}</span>}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  MetricStat — big number + label + optional sub                            */
/* -------------------------------------------------------------------------- */

interface MetricStatProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  align?: 'left' | 'right';
  emphasise?: boolean;
  mono?: boolean;
  hint?: string;
}

export function MetricStat({ label, value, sub, align = 'left', emphasise, mono, hint }: MetricStatProps) {
  return (
    <div className={cn('flex flex-col gap-0.5', align === 'right' && 'items-end text-right')}>
      <div className="text-[10px] uppercase tracking-widest text-text-muted font-medium">{label}</div>
      <div
        className={cn(
          'tabular font-semibold',
          mono && 'mono',
          emphasise ? 'text-2xl text-text' : 'text-base text-text'
        )}
        title={hint}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-text-2 tabular">{sub}</div>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  PLValue — signed money, coloured per §D1                                  */
/* -------------------------------------------------------------------------- */

export function PLValue({
  value,
  currency = 'USD',
  className,
  showSign = true,
}: {
  value: number;
  currency?: string;
  className?: string;
  showSign?: boolean;
}) {
  const positive = value > 0;
  const negative = value < 0;
  const color = positive ? 'var(--positive)' : negative ? 'var(--negative)' : 'var(--text-2)';
  const sign = showSign && positive ? '+' : negative ? '-' : '';
  const abs = Math.abs(value);
  const symbol = currency === 'USD' ? '$' : `${currency} `;
  return (
    <span className={cn('tabular font-medium', className)} style={{ color }}>
      {sign}
      {symbol}
      {abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  RValue — signed R                                                          */
/* -------------------------------------------------------------------------- */

export function RValue({ value, showSign = true, className }: { value: number | null; showSign?: boolean; className?: string }) {
  if (value === null) return <span className="text-text-muted tabular">—</span>;
  const positive = value > 0;
  const negative = value < 0;
  const color = positive ? 'var(--positive)' : negative ? 'var(--negative)' : 'var(--text-2)';
  return (
    <span className={cn('tabular font-medium', className)} style={{ color }}>
      {fmtR(value, { sign: showSign })}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  ConfidenceMeter — small horizontal bar with value                          */
/* -------------------------------------------------------------------------- */

export function ConfidenceMeter({ value, showValue = true, width = 60 }: { value: number; showValue?: boolean; width?: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const color = pct >= 80 ? 'var(--positive)' : pct >= 55 ? 'var(--warning)' : 'var(--negative)';
  return (
    <span className="inline-flex items-center gap-2 tabular">
      <span
        className="inline-block rounded-full bg-[color:var(--panel-3)] overflow-hidden"
        style={{ width, height: 4 }}
      >
        <span
          className="block h-full rounded-full transition-[width] duration-slow"
          style={{ width: `${pct}%`, background: color }}
        />
      </span>
      {showValue && <span className="text-xs text-text-2">{Math.round(pct)}%</span>}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  SampleSize                                                                 */
/* -------------------------------------------------------------------------- */

export function SampleSize({ n }: { n: number }) {
  return <span className="mono text-xs text-text-muted">n={n}</span>;
}

/* -------------------------------------------------------------------------- */
/*  PackageVersionChip                                                         */
/* -------------------------------------------------------------------------- */

import { GitBranch } from 'lucide-react';
/**
 * M-PKG-1: `version` is now optional. A package version asserts that a specific
 * strategy build is deployed and governing decisions; with no registry there is
 * no such fact, and rendering "v" followed by nothing — or worse a default —
 * would make the absence look like a value. The chip says so instead.
 */
export function PackageVersionChip({ version, hash }: { version?: number; hash?: string }) {
  const short = hash ? (hash.startsWith('sha256:') ? hash.slice(7, 15) : hash.slice(0, 8)) : '';
  if (version === undefined || version === null) {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-xs text-text-muted"
        data-testid="package-version-unavailable"
        title="No strategy-package registry exists, so no package version can be reported."
      >
        <GitBranch size={12} strokeWidth={2} />
        <span>no package registry</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-text-2">
      <GitBranch size={12} strokeWidth={2} />
      <span className="tabular">v{version}</span>
      {short && <span className="mono text-text-muted">{short}…</span>}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  CohortChip                                                                 */
/* -------------------------------------------------------------------------- */

import { sessionLabel } from '@/lib/utils';
export function CohortChip({
  session,
  structure,
  direction,
  size = 'sm',
}: {
  session: string;
  structure: string;
  direction: string;
  size?: 'sm' | 'md';
}) {
  const dirColor = direction.toLowerCase() === 'long' ? 'var(--positive)' : 'var(--negative)';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-sm border font-medium',
        size === 'sm' ? 'text-2xs px-1.5 h-5' : 'text-xs px-2 h-6'
      )}
      style={{
        borderColor: 'var(--border)',
        background: 'var(--panel-2)',
        color: 'var(--text)',
      }}
    >
      <span className="text-text-2">{sessionLabel(session)}</span>
      <span className="text-text-muted">·</span>
      <span className="text-text-2">{structure}</span>
      <span className="text-text-muted">·</span>
      <span style={{ color: dirColor }}>{direction.charAt(0).toUpperCase() + direction.slice(1).toLowerCase()}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  MarketStateBadge — Badge variant                                           */
/* -------------------------------------------------------------------------- */

/**
 * M-NODE-TEL-1: `state` is now optional. The badge rendered an authored regime
 * (`BullExpand`) at an authored confidence (96%) from an authored model
 * (`regime@2.3.0`). No node publishes market state, so absence must read as
 * absence — not as a neutral regime and not as zero confidence, either of which
 * would be a claim about the market.
 */
export function MarketStateBadge({ state, confidence, confirmed = true }: { state?: string; confidence?: number; confirmed?: boolean }) {
  if (!state) {
    return (
      <span
        className="inline-flex items-center gap-1 text-2xs text-text-muted"
        data-testid="market-state-unavailable"
        title="No market-state model publishes to this process. Regime and confidence are unavailable — not neutral, and not zero."
      >
        market state unavailable
      </span>
    );
  }
  return <MarketStateBadgeInner state={state} confidence={confidence} confirmed={confirmed} />;
}
function MarketStateBadgeInner({ state, confidence, confirmed = true }: { state: string; confidence?: number; confirmed?: boolean }) {
  const color = marketStateVar(state);
  return (
    <Badge variant="marketState" color={color} glyph={<span className="mono text-2xs">{marketStateGlyph(state)}</span>}>
      {marketStateLabel(state)}
      {confidence !== undefined && (
        <span className="ml-1 text-text-muted">({Math.round(confidence * 100)}%)</span>
      )}
      {!confirmed && <span className="ml-1 text-text-muted">·un</span>}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/*  ValidationBadge — Badge variant                                            */
/* -------------------------------------------------------------------------- */

export function ValidationBadgeChip({ badge }: { badge: string }) {
  return (
    <Badge variant="validation" color={badgeColor(badge)} glyph={<CircleCheck size={10} strokeWidth={2.5} />}>
      {badge}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/*  ProvenanceChip — where a displayed value came from (UI-0)                   */
/* -------------------------------------------------------------------------- */

/**
 * The single visual treatment for data provenance. Any operator-facing value that
 * is not genuinely emitted by a live node or a real market source must be rendered
 * next to one of these. Uses the ONE Badge primitive (no new badge component).
 */
export function ProvenanceChip({
  provenance,
  detail,
  className,
}: {
  provenance: DataProvenance;
  detail?: string;
  className?: string;
}) {
  const tone = PROVENANCE_TONE[provenance];
  const color =
    tone === 'trusted'
      ? 'var(--positive)'
      : tone === 'caution'
        ? 'var(--caution)'
        : tone === 'critical'
          ? 'var(--negative)'
          : 'var(--text-muted)';
  const glyph =
    tone === 'trusted' ? <CircleCheck size={10} strokeWidth={2.5} /> : tone === 'inert' ? <CircleSlash size={10} /> : <CircleAlert size={10} />;
  return (
    <Badge
      variant="neutral"
      color={color}
      glyph={glyph}
      outline={tone === 'inert'}
      className={className}
      title={detail ? `${PROVENANCE_HINT[provenance]} ${detail}` : PROVENANCE_HINT[provenance]}
    >
      {PROVENANCE_LABEL[provenance]}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/*  EligibilityBadge — Badge variant                                           */
/* -------------------------------------------------------------------------- */

export function EligibilityBadge({ action, allowed }: { action: string; allowed: boolean }) {
  const color = allowed ? eligibilityColor(action) : 'var(--negative)';
  const glyph = allowed ? <CircleDot size={10} /> : <CircleSlash size={10} />;
  return (
    <Badge variant="eligibility" color={color} glyph={glyph}>
      {eligibilityLabel(action)}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/*  LaneChip                                                                   */
/* -------------------------------------------------------------------------- */

export function LaneChip({ lane }: { lane: string }) {
  return (
    <Badge variant="lane" color={laneColor(lane)}>
      {laneLabel(lane)}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/*  TimestampUTC                                                               */
/* -------------------------------------------------------------------------- */

import { fmtRelative } from '@/lib/format';
export function TimestampUTC({ iso, mode = 'relative' }: { iso: string; mode?: 'relative' | 'absolute' }) {
  const abs = new Date(iso).toISOString().replace('.000Z', 'Z');
  const rel = fmtRelative(iso);
  return (
    <span title={abs} className={cn(mode === 'absolute' ? 'mono' : '', 'text-text-2')}>
      {mode === 'relative' ? rel : abs}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  KeyValueGrid                                                               */
/* -------------------------------------------------------------------------- */

export function KeyValueGrid({ items }: { items: Array<{ label: string; value: ReactNode; mono?: boolean }> }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5">
      {items.map((item, i) => (
        <div key={i} className="contents">
          <dt className="text-xs text-text-muted uppercase tracking-wider">{item.label}</dt>
          <dd className={cn('text-sm text-text tabular', item.mono && 'mono')}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/* -------------------------------------------------------------------------- */
/*  WhyButton — universal decision-chain opener                                */
/* -------------------------------------------------------------------------- */

import { HelpCircle } from 'lucide-react';
export function WhyButton({ onClick, size = 'sm' }: { onClick: () => void; size?: 'sm' | 'md' }) {
  return (
    <button
      onClick={onClick}
      title="Why? — open decision chain"
      className={cn(
        'inline-flex items-center gap-1 rounded-sm border border-transparent text-text-muted hover:text-text hover:border-[color:var(--border)] transition-colors duration-fast',
        size === 'sm' ? 'h-5 px-1.5 text-2xs' : 'h-6 px-2 text-xs'
      )}
    >
      <HelpCircle size={size === 'sm' ? 11 : 13} strokeWidth={2} />
      Why?
    </button>
  );
}
