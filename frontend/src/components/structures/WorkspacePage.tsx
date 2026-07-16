import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { Panel } from './Panel';

/* -------------------------------------------------------------------------- */
/*  WorkspacePage — page title, toolbar, content grid, optional inspector      */
/*  Every workspace uses this. Consistent skeleton, variable emphasis.         */
/* -------------------------------------------------------------------------- */

interface WorkspacePageProps {
  title: ReactNode;
  subtitle?: ReactNode;
  toolbar?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
  /** Renders inside a right-side aside with fixed width. */
  aside?: ReactNode;
  asideWidth?: number;
}

export function WorkspacePage({
  title,
  subtitle,
  toolbar,
  right,
  children,
  className,
  padded = true,
  aside,
  asideWidth = 320,
}: WorkspacePageProps) {
  const body = (
    <div className={cn('flex flex-col h-full min-h-0', className)}>
      <header
        className="flex items-center gap-4 px-6 pt-5 pb-4 shrink-0"
        style={{ borderBottom: '1px solid var(--border-subtle)' }}
      >
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-text tracking-tight truncate">{title}</h1>
          {subtitle && <p className="text-xs text-text-muted mt-0.5 truncate">{subtitle}</p>}
        </div>
        {right && <div className="ml-auto flex items-center gap-2 shrink-0">{right}</div>}
      </header>
      {toolbar && (
        <div
          className="flex items-center gap-2 px-6 h-10 border-b shrink-0"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          {toolbar}
        </div>
      )}
      <div className={cn('flex-1 min-h-0 overflow-auto', padded && 'p-4')}>{children}</div>
    </div>
  );

  if (!aside) return body;
  return (
    <div className="grid h-full min-h-0" style={{ gridTemplateColumns: `1fr ${asideWidth}px` }}>
      {body}
      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        {aside}
      </aside>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Toolbar helpers                                                            */
/* -------------------------------------------------------------------------- */

export function ToolbarGroup({ children }: { children: ReactNode }) {
  return <div className="flex items-center gap-1">{children}</div>;
}

export function ToolbarSpacer() {
  return <div className="flex-1" />;
}

export function ToolbarDivider() {
  return <div className="h-5 w-px" style={{ background: 'var(--border-subtle)' }} />;
}

export function ToolbarLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-[10px] uppercase tracking-widest text-text-muted font-medium">{children}</span>
  );
}

export function ToolbarChip({
  active,
  onClick,
  children,
  count,
}: {
  active?: boolean;
  onClick?: () => void;
  children: ReactNode;
  count?: number;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 h-7 px-2.5 rounded-sm text-xs font-medium border transition-colors duration-fast',
        active
          ? 'bg-[color:var(--panel)] text-text border-[color:var(--border)]'
          : 'bg-transparent text-text-2 border-transparent hover:text-text hover:bg-[color:var(--panel)]'
      )}
    >
      {children}
      {count !== undefined && (
        <span
          className="ml-1 rounded-sm px-1 py-0.5 text-[10px] mono"
          style={{ background: 'var(--panel-3)', color: 'var(--text-muted)' }}
        >
          {count}
        </span>
      )}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/*  Placeholder primitives — for pages that have no real data yet              */
/* -------------------------------------------------------------------------- */

/** Marks a section as "not wired yet" — quiet, honest, doesn't look broken. */
export function ComingSoon({
  title = 'Coming in a later phase',
  description,
  compact = false,
}: {
  title?: string;
  description?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        'rounded-md border flex items-start gap-3',
        compact ? 'p-3' : 'p-4'
      )}
      style={{
        borderColor: 'var(--border-subtle)',
        background: 'color-mix(in srgb, var(--primary) 5%, var(--panel))',
      }}
    >
      <div
        className="w-1 self-stretch rounded-full shrink-0"
        style={{ background: 'var(--primary)', opacity: 0.4 }}
      />
      <div className="min-w-0">
        <div className="text-sm font-medium text-text">{title}</div>
        {description && <div className="text-xs text-text-muted mt-1">{description}</div>}
      </div>
    </div>
  );
}

/** Static shimmering skeleton row bank — used inside placeholder panels. */
export function SkeletonRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-2', className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-4 rounded-sm"
          style={{
            background: 'var(--panel-2)',
            opacity: 0.6 + Math.sin(i) * 0.15,
            width: `${60 + Math.round(Math.sin(i * 2) * 25 + 25)}%`,
          }}
        />
      ))}
    </div>
  );
}

export function PlaceholderTable({
  columns,
  rowCount = 6,
}: {
  columns: string[];
  rowCount?: number;
}) {
  return (
    <div className="w-full">
      <div
        className="grid gap-3 py-2 px-3 border-b text-2xs uppercase tracking-widest text-text-muted"
        style={{
          borderColor: 'var(--border-subtle)',
          gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))`,
          background: 'var(--panel-2)',
        }}
      >
        {columns.map((c) => (
          <span key={c}>{c}</span>
        ))}
      </div>
      {Array.from({ length: rowCount }).map((_, i) => (
        <div
          key={i}
          className="grid gap-3 py-2 px-3 border-b"
          style={{
            borderColor: 'var(--border-subtle)',
            gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))`,
          }}
        >
          {columns.map((_c, j) => (
            <span
              key={j}
              className="h-3 rounded-sm mono"
              style={{
                background: 'var(--panel-2)',
                opacity: 0.55 + Math.sin(i * 2 + j) * 0.15,
                width: `${50 + ((i * 13 + j * 7) % 40)}%`,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function PlaceholderChart({
  height = 200,
  variant = 'line',
}: {
  height?: number;
  variant?: 'line' | 'bar' | 'area';
}) {
  const points = 40;
  const values = Array.from({ length: points }, (_, i) => {
    const v = Math.sin(i * 0.35) * 20 + Math.sin(i * 0.7) * 8 + 50;
    return Math.max(6, Math.min(96, v));
  });
  const path = values.map((v, i) => `${i === 0 ? 'M' : 'L'} ${(i / (points - 1)) * 100} ${100 - v}`).join(' ');
  return (
    <div
      className="w-full relative rounded-md border overflow-hidden"
      style={{
        borderColor: 'var(--border-subtle)',
        background: 'var(--panel-2)',
        height,
      }}
    >
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-full h-full">
        <defs>
          <linearGradient id="pcArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#4C82F7" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#4C82F7" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[20, 40, 60, 80].map((y) => (
          <line key={y} x1="0" x2="100" y1={y} y2={y} stroke="var(--border-subtle)" strokeWidth="0.15" />
        ))}
        {variant === 'bar' ? (
          values.map((v, i) => (
            <rect
              key={i}
              x={(i / points) * 100 + 0.4}
              y={100 - v}
              width={100 / points - 0.8}
              height={v}
              fill={i % 2 === 0 ? '#3fb27f' : '#e5565b'}
              opacity="0.35"
            />
          ))
        ) : (
          <>
            {variant === 'area' && (
              <path d={`${path} L 100 100 L 0 100 Z`} fill="url(#pcArea)" />
            )}
            <path d={path} fill="none" stroke="#4C82F7" strokeWidth="0.4" vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>
      <div className="absolute top-2 left-3 text-[10px] uppercase tracking-widest text-text-muted">
        Sample series · not wired
      </div>
    </div>
  );
}

export function PlaceholderMetricGrid({
  items,
}: {
  items: Array<{ label: string; value: string; sub?: string }>;
}) {
  return (
    <div className="grid grid-cols-4 gap-4">
      {items.map((it, i) => (
        <div
          key={i}
          className="rounded-md border p-3"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        >
          <div className="text-[10px] uppercase tracking-widest text-text-muted">{it.label}</div>
          <div className="text-lg font-semibold text-text mono mt-1">{it.value}</div>
          {it.sub && <div className="text-2xs text-text-muted mt-0.5">{it.sub}</div>}
        </div>
      ))}
    </div>
  );
}

/** Content-agnostic panel used when a section has no real data yet. */
export function PlaceholderPanel({
  title,
  description,
  height = 200,
  variant = 'chart',
  columns,
}: {
  title?: string;
  description?: string;
  height?: number;
  variant?: 'chart' | 'table' | 'rows' | 'empty';
  columns?: string[];
}) {
  return (
    <Panel title={title} subtitle={description} bodyClassName="p-3">
      {variant === 'chart' && <PlaceholderChart height={height} />}
      {variant === 'table' && <PlaceholderTable columns={columns ?? ['Field', 'Value', 'Δ']} />}
      {variant === 'rows' && <SkeletonRows rows={6} />}
      {variant === 'empty' && (
        <div className="text-sm text-text-muted italic py-4 text-center">Placeholder — data lands in a later phase</div>
      )}
    </Panel>
  );
}
