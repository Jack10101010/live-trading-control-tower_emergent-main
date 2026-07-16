import type { ReactNode } from 'react';
import { Button, IconButton } from '@/components/primitives/Button';
import { useCommand } from '@/hooks/useCommand';
import type { Command } from '@/lib/commands';
import { cn } from '@/lib/utils';

/**
 * ActionBar — the SINGLE action affordance. Every operator action row (orders,
 * trades, inspector) is one ActionBar driven by a list of items. A `command`
 * item routes through the one command dispatcher (→ ConfirmDialog); an
 * `onClick` item is a pure UI action (e.g. open Inspector). No duplicated
 * command-button logic anywhere.
 */
export interface ActionItem {
  key: string;
  label: string;
  icon?: ReactNode;
  command?: Command;
  onClick?: () => void;
  variant?: 'ghost' | 'outline' | 'secondary' | 'danger' | 'primary';
  disabled?: boolean;
}

export function ActionBar({
  actions,
  iconOnly = false,
  size = 'sm',
  className,
}: {
  actions: ActionItem[];
  iconOnly?: boolean;
  size?: 'sm' | 'md';
  className?: string;
}) {
  const dispatch = useCommand();

  const run = (a: ActionItem) => {
    if (a.disabled) return;
    if (a.command) dispatch(a.command);
    else a.onClick?.();
  };

  return (
    <div className={cn('flex items-center gap-1.5 flex-wrap', className)} onClick={(e) => e.stopPropagation()}>
      {actions.map((a) =>
        iconOnly ? (
          <IconButton
            key={a.key}
            ariaLabel={a.label}
            size={size}
            variant={a.variant ?? 'ghost'}
            disabled={a.disabled}
            onClick={() => run(a)}
            data-testid={`action-${a.key}`}
          >
            {a.icon}
          </IconButton>
        ) : (
          <Button
            key={a.key}
            size={size}
            variant={a.variant ?? 'ghost'}
            icon={a.icon}
            disabled={a.disabled}
            onClick={() => run(a)}
            data-testid={`action-${a.key}`}
          >
            {a.label}
          </Button>
        )
      )}
    </div>
  );
}
