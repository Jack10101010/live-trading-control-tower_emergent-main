import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';
import { provenanceBadge, provenanceTone, type PanelProvenance } from '@/lib/cardProvenance';

interface PanelProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  dense?: boolean;
  scroll?: boolean;
  /**
   * REQUIRED data-provenance classification (temporary migration aid).
   * Pure chrome (nav, empty forms, decorative containers) uses 'none'.
   * TypeScript enforces that no data card can omit its classification.
   */
  provenance: PanelProvenance;
}

/** Border + badge styling for the temporary GREEN/RED provenance system. */
function provenanceStyle(tone: 'green' | 'red' | 'none'): {
  border: string; glow: string | undefined; badgeBg: string; badgeFg: string;
} | null {
  if (tone === 'none') return null;
  const color = tone === 'green' ? 'var(--positive)' : 'var(--negative)';
  return {
    border: color,
    glow: `0 0 0 1px ${tone === 'green' ? 'var(--positive)' : 'var(--negative)'} inset`,
    badgeBg: color,
    badgeFg: 'var(--panel)',
  };
}

/** Small LIVE / NON-LIVE badge rendered in the shared header slot. */
export function ProvenanceBadge({ provenance }: { provenance: PanelProvenance }) {
  const label = provenanceBadge(provenance);
  if (!label) return null;
  const tone = provenanceTone(provenance);
  const s = provenanceStyle(tone)!;
  return (
    <span
      data-testid="card-provenance-badge"
      data-provenance={provenance}
      className="text-[9px] font-bold tracking-widest px-1.5 py-0.5 rounded-sm shrink-0"
      style={{ background: s.badgeBg, color: s.badgeFg }}
    >
      {label}
    </span>
  );
}

/**
 * Panel — the fundamental container. Header + body + optional actions.
 * Consistency mandate: every workspace section uses this.
 */
export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
  dense = false,
  scroll = false,
  provenance,
}: PanelProps) {
  const tone = provenanceTone(provenance);
  const pstyle = provenanceStyle(tone);
  return (
    <section
      data-provenance={provenance}
      className={cn(
        'flex flex-col border rounded-md overflow-hidden',
        className
      )}
      style={{
        borderColor: pstyle ? pstyle.border : 'var(--border-subtle)',
        boxShadow: pstyle?.glow,
        background: 'var(--panel)',
      }}
    >
      {(title || actions || pstyle) && (
        <header
          className={cn(
            'flex items-center justify-between border-b',
            dense ? 'px-3 h-8' : 'px-4 h-10'
          )}
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        >
          <div className="flex items-baseline gap-3 min-w-0">
            {title && (
              <h3
                className={cn(
                  'font-semibold uppercase tracking-widest text-text-2 truncate',
                  dense ? 'text-[10px]' : 'text-[11px]'
                )}
              >
                {title}
              </h3>
            )}
            {subtitle && <span className="text-xs text-text-muted truncate">{subtitle}</span>}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {actions && <div className="flex items-center gap-1 shrink-0">{actions}</div>}
            <ProvenanceBadge provenance={provenance} />
          </div>
        </header>
      )}
      <div
        className={cn(
          'flex-1',
          dense ? 'p-2' : 'p-4',
          scroll && 'overflow-auto',
          bodyClassName
        )}
      >
        {children}
      </div>
    </section>
  );
}

interface CardProps {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
  interactive?: boolean;
  selected?: boolean;
  'data-testid'?: string;
}

export function Card({ children, className, onClick, interactive, selected, ...rest }: CardProps) {
  return (
    <div
      onClick={onClick}
      data-testid={rest['data-testid']}
      className={cn(
        'rounded-md border transition-colors duration-fast ease-enter',
        interactive && 'cursor-pointer hover:bg-[color:var(--panel-2)]',
        selected && 'ring-1 ring-[color:var(--focus)]',
        className
      )}
      style={{
        borderColor: selected ? 'var(--focus)' : 'var(--border-subtle)',
        background: 'var(--panel)',
      }}
    >
      {children}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon,
  action,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-10 px-6 text-center">
      {icon && <div className="text-text-muted mb-3">{icon}</div>}
      <div className="text-sm text-text font-medium">{title}</div>
      {description && <div className="text-xs text-text-muted mt-1 max-w-sm">{description}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({
  title = 'Something went wrong',
  description,
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-8 px-4 text-center">
      <div className="text-sm text-[color:var(--negative)] font-medium">{title}</div>
      {description && <div className="text-xs text-text-muted mt-1 max-w-md">{description}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

/** DiffView — before/after presentation used by DraftDiff etc. */
export function DiffView({
  before,
  after,
  label,
}: {
  before: ReactNode;
  after: ReactNode;
  label?: string;
}) {
  return (
    <div className="grid grid-cols-[max-content_max-content_max-content] items-center gap-2 text-xs tabular">
      {label && <span className="text-text-muted uppercase tracking-wider">{label}</span>}
      <span className="text-text-muted line-through">{before}</span>
      <span className="text-text-muted">→</span>
      <span className="text-text font-semibold" style={{ color: 'var(--draft)' }}>
        {after}
      </span>
    </div>
  );
}

/**
 * ProvenanceFrame — provenance border/badge for components that own their own
 * internal layout (OperationalDashboard, TradeLedgerPanel, LiveRuntimePanel…).
 * Wrap at the mount site; the child renders unchanged inside the frame.
 */
export function ProvenanceFrame({
  provenance,
  children,
  className,
}: {
  provenance: PanelProvenance;
  children: ReactNode;
  className?: string;
}) {
  const tone = provenanceTone(provenance);
  if (tone === 'none') return <>{children}</>;
  const color = tone === 'green' ? 'var(--positive)' : 'var(--negative)';
  return (
    <div
      data-provenance={provenance}
      className={cn('relative rounded-md border', className)}
      style={{ borderColor: color, boxShadow: `0 0 0 1px ${color} inset` }}
    >
      {/* straddle the frame edge so the badge never covers card content */}
      <div className="absolute right-2 -top-2 z-10">
        <ProvenanceBadge provenance={provenance} />
      </div>
      {children}
    </div>
  );
}
