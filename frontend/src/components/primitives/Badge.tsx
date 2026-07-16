import { cn } from '@/lib/utils';
import type { CSSProperties, ReactNode } from 'react';

export type BadgeVariant =
  | 'status'
  | 'validation'
  | 'target'
  | 'risk'
  | 'marketState'
  | 'health'
  | 'lane'
  | 'recommendation'
  | 'eligibility'
  | 'mode'
  | 'neutral'
  | 'ghost'
  | 'live'
  | 'draft';

interface BadgeProps {
  variant?: BadgeVariant;
  color?: string; // override
  bg?: string;
  glyph?: ReactNode;
  children: ReactNode;
  size?: 'sm' | 'md';
  outline?: boolean;
  className?: string;
  title?: string;
}

/**
 * ONE Badge primitive, variant-driven. Never spawn `XyzBadge`.
 * Triple-encoded: colour + label + glyph (§D1).
 */
export function Badge({
  variant = 'neutral',
  color,
  bg,
  glyph,
  children,
  size = 'sm',
  outline = false,
  className,
  title,
}: BadgeProps) {
  const c = color ?? variantColor(variant);
  const style: CSSProperties = outline
    ? {
        color: c,
        borderColor: c,
        backgroundColor: 'transparent',
      }
    : {
        color: c,
        borderColor: `${c}44`,
        backgroundColor: bg ?? tint(c),
      };

  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-sm border font-medium whitespace-nowrap tabular',
        size === 'sm' ? 'text-2xs px-1.5 py-0.5 leading-none h-5' : 'text-xs px-2 py-0.5 leading-none h-6',
        className
      )}
      style={style}
    >
      {glyph && <span className="inline-flex items-center leading-none">{glyph}</span>}
      {children}
    </span>
  );
}

function variantColor(v: BadgeVariant): string {
  switch (v) {
    case 'validation':
      return 'var(--validated)';
    case 'health':
      return 'var(--healthy)';
    case 'lane':
      return 'var(--primary)';
    case 'recommendation':
      return 'var(--recommendation)';
    case 'eligibility':
      return 'var(--primary)';
    case 'marketState':
      return 'var(--bull)';
    case 'mode':
      return 'var(--mode-live)';
    case 'ghost':
      return 'var(--ghost)';
    case 'live':
      return 'var(--live)';
    case 'draft':
      return 'var(--draft)';
    case 'risk':
      return 'var(--warning)';
    case 'target':
      return 'var(--primary)';
    case 'status':
      return 'var(--primary)';
    default:
      return 'var(--text-2)';
  }
}

function tint(hex: string): string {
  // Hex or CSS var — if var, use a fixed low-alpha wrapper via rgba fallback isn't possible;
  // provide a subtle background using color-mix if supported, else transparent tint.
  return `color-mix(in srgb, ${hex} 12%, transparent)`;
}
