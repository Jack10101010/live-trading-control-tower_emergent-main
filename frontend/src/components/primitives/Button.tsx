import { cn } from '@/lib/utils';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline';
type Size = 'sm' | 'md' | 'lg';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
  trailing?: ReactNode;
  fullWidth?: boolean;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  trailing,
  fullWidth,
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      className={cn(
        'inline-flex items-center gap-2 rounded-md border font-medium transition-colors duration-fast ease-enter',
        'disabled:opacity-40 disabled:cursor-not-allowed',
        size === 'sm' && 'h-7 px-2 text-xs',
        size === 'md' && 'h-8 px-3 text-sm',
        size === 'lg' && 'h-9 px-4 text-sm',
        variant === 'primary' &&
          'bg-primary border-primary text-[color:var(--on-primary)] hover:brightness-110',
        variant === 'secondary' &&
          'bg-[color:var(--panel-2)] border-[color:var(--border)] text-text hover:bg-[color:var(--panel-3)]',
        variant === 'ghost' &&
          'bg-transparent border-transparent text-text-2 hover:bg-[color:var(--panel-2)] hover:text-text',
        variant === 'outline' &&
          'bg-transparent border-[color:var(--border)] text-text hover:bg-[color:var(--panel-2)]',
        variant === 'danger' &&
          'bg-[color:var(--negative)] border-[color:var(--negative)] text-white hover:brightness-110',
        fullWidth && 'w-full justify-center',
        className
      )}
      {...rest}
    >
      {icon && <span className="inline-flex items-center leading-none">{icon}</span>}
      {children}
      {trailing && <span className="inline-flex items-center leading-none">{trailing}</span>}
    </button>
  );
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  size?: Size;
  variant?: Variant;
  ariaLabel: string;
}

export function IconButton({
  size = 'md',
  variant = 'ghost',
  ariaLabel,
  className,
  children,
  ...rest
}: IconButtonProps) {
  return (
    <button
      aria-label={ariaLabel}
      title={ariaLabel}
      className={cn(
        'inline-flex items-center justify-center rounded-md border transition-colors duration-fast ease-enter',
        size === 'sm' && 'h-6 w-6',
        size === 'md' && 'h-7 w-7',
        size === 'lg' && 'h-8 w-8',
        variant === 'ghost' &&
          'bg-transparent border-transparent text-text-2 hover:bg-[color:var(--panel-2)] hover:text-text',
        variant === 'secondary' &&
          'bg-[color:var(--panel-2)] border-[color:var(--border)] text-text hover:bg-[color:var(--panel-3)]',
        variant === 'primary' &&
          'bg-primary border-primary text-[color:var(--on-primary)]',
        variant === 'outline' &&
          'bg-transparent border-[color:var(--border)] text-text hover:bg-[color:var(--panel-2)]',
        variant === 'danger' &&
          'bg-transparent border-transparent text-[color:var(--negative)] hover:bg-[color:var(--panel-2)]',
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
