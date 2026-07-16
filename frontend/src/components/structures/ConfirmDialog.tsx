import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ShieldAlert, X } from 'lucide-react';
import { Button } from '@/components/primitives/Button';
import { useCommandStore } from '@/store/commandStore';
import { commandMeta } from '@/lib/commands';
import { executeCommand } from '@/hooks/useCommand';

/**
 * ConfirmDialog — the ONE blocking modal for the whole app. Reads the pending
 * command from the command store; renders confirm / confirm+reason; executes
 * via the single dispatcher. Never spawn another modal for a command.
 */
export function ConfirmDialog() {
  const pending = useCommandStore((s) => s.pending);
  const clear = useCommandStore((s) => s.clear);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setReason('');
    setBusy(false);
  }, [pending]);

  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) clear();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pending, busy, clear]);

  if (!pending) return null;
  const meta = commandMeta[pending.name];
  const needsReason = meta.confirmation === 'confirm+reason';
  const canConfirm = !busy && (!needsReason || reason.trim().length > 0);

  const onConfirm = async () => {
    setBusy(true);
    await executeCommand(pending, needsReason ? reason.trim() : undefined);
    clear();
  };

  return createPortal(
    <div className="fixed inset-0 z-[400] flex items-center justify-center" role="dialog" aria-modal="true" aria-label={meta.label}>
      <div className="absolute inset-0" style={{ background: 'rgba(0,0,0,0.55)' }} onClick={() => !busy && clear()} />
      <div
        className="relative w-[440px] max-w-[92vw] rounded-lg border ct-elev-3 overflow-hidden"
        style={{ background: 'var(--bg-elevated)', borderColor: 'var(--border)' }}
      >
        <header
          className="flex items-center gap-2 px-4 h-11 border-b"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          {meta.destructive ? (
            <ShieldAlert size={15} className="text-[color:var(--negative)]" />
          ) : (
            <AlertTriangle size={15} className="text-[color:var(--warning)]" />
          )}
          <span className="text-sm font-semibold text-text">{meta.label}</span>
          <button
            className="ml-auto text-text-muted hover:text-text"
            onClick={() => !busy && clear()}
            aria-label="Cancel"
          >
            <X size={15} />
          </button>
        </header>

        <div className="px-4 py-4 space-y-3">
          <p className="text-sm text-text-2">{meta.summary}</p>

          <div
            className="rounded-md border p-2 text-2xs mono text-text-muted"
            style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
          >
            {pending.name}
            {Object.keys(pending.payload).length > 0 && (
              <span> · {JSON.stringify(pending.payload)}</span>
            )}
          </div>

          {needsReason && (
            <label className="block space-y-1">
              <span className="text-2xs uppercase tracking-widest text-text-muted">Reason (required · audited)</span>
              <textarea
                autoFocus
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={2}
                placeholder="Why are you doing this?"
                className="w-full rounded-md border px-2 py-1.5 text-sm text-text bg-[color:var(--panel-2)] outline-none focus-visible:border-[color:var(--focus)]"
                style={{ borderColor: 'var(--border)' }}
              />
            </label>
          )}

          <div
            className="flex items-center gap-1.5 text-2xs text-text-muted rounded-md px-2 py-1.5 border"
            style={{ borderColor: 'var(--border-subtle)' }}
          >
            <span className="mono">MOCK</span>
            <span>· executes against the mock backend only — no live broker action.</span>
          </div>
        </div>

        <footer
          className="flex items-center justify-end gap-2 px-4 h-12 border-t"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          <Button variant="ghost" size="sm" onClick={() => clear()} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant={meta.destructive ? 'danger' : 'primary'}
            size="sm"
            onClick={onConfirm}
            disabled={!canConfirm}
            data-testid="confirm-command"
          >
            {busy ? 'Working…' : meta.label}
          </Button>
        </footer>
      </div>
    </div>,
    document.body
  );
}
