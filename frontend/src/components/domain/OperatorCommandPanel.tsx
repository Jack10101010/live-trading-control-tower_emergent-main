/**
 * UI-17 — read-only operator controls.
 *
 * A compact panel with exactly three actions, each of which asks the node a
 * READ-ONLY question: request health, request telemetry, and an optional no-op
 * diagnostic. There is NO execution control here — no pause, resume, arm, order,
 * cancel or kill — and none is representable: the only thing a button can do is POST
 * one of the three permitted command types to `/api/operator/commands`.
 *
 * Truthfulness rules honoured here:
 *   - NO optimistic success. Nothing is shown as accepted/completed until the
 *     backend returns the mapped lifecycle state. A disabled or unreachable transport
 *     is displayed as exactly that, never as success.
 *   - Duplicate clicks are prevented while a submission is in flight (every button is
 *     disabled and marked aria-busy), so a double-click cannot fire two actions.
 *   - A FRESH idempotency key is generated per intentional action (in the api layer),
 *     so a genuine retry de-duplicates but a new action is distinct.
 *   - NO token handling in the browser: the panel sends no credential and stores none.
 *   - Accessible loading and error states: buttons expose aria-busy, the result is a
 *     role=status live region, and a rejection/failure is announced as role=alert with
 *     its stable machine code preserved beside readable text.
 */
import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  api,
  OperatorCommandError,
  type OperatorCommandType,
  type OperatorCommandView,
} from '@/lib/api';

/** The three permitted actions. Labels are read-only questions, never commands. */
const ACTIONS: Array<{ type: OperatorCommandType; label: string; hint: string }> = [
  { type: 'request_health', label: 'Request health', hint: 'Ask the node for a health check.' },
  { type: 'request_telemetry', label: 'Request telemetry', hint: 'Ask the node for a telemetry snapshot.' },
  { type: 'noop', label: 'No-op diagnostic', hint: 'A round-trip that changes nothing.' },
];

/** Per-state presentation. Colour is never the only signal — the state word and a
 *  glyph accompany it, and severity is stated in the accessible summary. */
const STATE_META: Record<string, { glyph: string; color: string; label: string }> = {
  pending: { glyph: '·', color: 'var(--text-muted)', label: 'PENDING' },
  accepted: { glyph: '◐', color: 'var(--caution, var(--warning))', label: 'ACCEPTED' },
  rejected: { glyph: '■', color: 'var(--negative)', label: 'REJECTED' },
  expired: { glyph: '□', color: 'var(--text-muted)', label: 'EXPIRED' },
  completed: { glyph: '●', color: 'var(--positive)', label: 'COMPLETED' },
  failed: { glyph: '■', color: 'var(--negative)', label: 'FAILED' },
};

function fmt(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toISOString().replace('T', ' ').replace('Z', 'Z');
}

function ResultLine({ view }: { view: OperatorCommandView }) {
  const meta = STATE_META[view.state] ?? { glyph: '·', color: 'var(--text-muted)', label: view.state.toUpperCase() };
  const reason = view.transport?.reason ?? view.outcomeState ?? null;
  const disabledOrUnreachable = !view.enabled || view.transport?.reason === 'command_transport_disabled';
  return (
    <div
      className="mt-2 text-2xs border rounded-sm px-2 py-1"
      style={{ borderColor: `${meta.color}55` }}
      data-testid="operator-command-result"
      data-state={view.state}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-baseline gap-2">
        <span aria-hidden style={{ color: meta.color }}>{meta.glyph}</span>
        <span className="mono font-semibold" style={{ color: meta.color }}>{meta.label}</span>
        <span className="text-text-muted">{view.commandType}</span>
        {/* acknowledgement and completion are shown as DISTINCT facts. */}
        <span className="text-text-muted">
          ack {view.acknowledged ? (view.accepted ? 'accepted' : 'rejected') : 'none'}
          {' · '}completed {view.completed ? 'yes' : 'no'}
        </span>
      </div>
      <div className="mt-0.5 text-text-muted flex flex-wrap gap-x-3">
        {reason && (
          <span>
            reason <code className="mono text-text-2">{reason}</code>
          </span>
        )}
        <span>
          updated <time className="mono" dateTime={view.updatedAt}>{fmt(view.updatedAt)}</time>
        </span>
        <span>expires <time className="mono" dateTime={view.expiresAt}>{fmt(view.expiresAt)}</time></span>
      </div>
      {disabledOrUnreachable && (
        <div className="mt-0.5" data-testid="operator-disabled-note">
          Remote command transport is disabled — the command was recorded but nothing was
          transmitted. This is the default, safe state.
        </div>
      )}
      <span className="sr-only">
        {`Command ${view.commandType} is ${meta.label}. Acknowledged: ${
          view.acknowledged ? (view.accepted ? 'accepted' : 'rejected') : 'no'
        }. Completed: ${view.completed ? 'yes' : 'no'}.${reason ? ` Reason: ${reason}.` : ''}`}
      </span>
    </div>
  );
}

export function OperatorCommandPanel() {
  const [lastType, setLastType] = useState<OperatorCommandType | null>(null);
  const mutation = useMutation<OperatorCommandView, Error, OperatorCommandType>({
    mutationFn: (type) => api.operatorSubmitCommand(type),
  });
  // One in-flight submission at a time: disable ALL buttons while pending, so an
  // accidental double-click (or a second action mid-flight) cannot fire twice.
  const busy = mutation.isPending;

  const submit = (type: OperatorCommandType) => {
    if (busy) return;
    setLastType(type);
    mutation.mutate(type);
  };

  const rejection =
    mutation.error instanceof OperatorCommandError ? mutation.error : null;

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Operator controls"
      data-testid="operator-command-panel"
    >
      <header className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Operator controls</h3>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: 'var(--text-muted)', color: 'var(--text-muted)' }}
          data-testid="operator-scope"
        >
          READ-ONLY
        </span>
      </header>

      <p className="text-2xs text-text-muted mb-2">
        Read-only diagnostics only. These ask the node a question; none of them changes
        node state, trades, arming or execution. Disabled by default until a transport
        is deliberately configured.
      </p>

      <div className="flex flex-wrap gap-2" role="group" aria-label="Read-only commands">
        {ACTIONS.map((action) => (
          <button
            key={action.type}
            type="button"
            onClick={() => submit(action.type)}
            disabled={busy}
            aria-busy={busy && lastType === action.type}
            title={action.hint}
            data-testid={`operator-btn-${action.type}`}
            className="text-2xs px-2 py-1 rounded-sm border disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ borderColor: 'var(--border)', color: 'var(--text-2)' }}
          >
            {busy && lastType === action.type ? `${action.label}…` : action.label}
          </button>
        ))}
      </div>

      {busy && (
        <div className="mt-2 text-2xs text-text-muted" role="status" aria-live="polite"
             data-testid="operator-loading">
          Submitting… waiting for the node to respond. Nothing is shown as accepted until
          the backend answers.
        </div>
      )}

      {rejection && (
        <div
          className="mt-2 text-2xs border rounded-sm px-2 py-1"
          style={{ borderColor: 'var(--negative)', color: 'var(--negative)' }}
          role="alert"
          data-testid="operator-command-rejection"
        >
          Rejected — <code className="mono">{rejection.code}</code>
          {rejection.detail ? ` · ${rejection.detail}` : ''}
        </div>
      )}

      {mutation.isError && !rejection && (
        <div
          className="mt-2 text-2xs border rounded-sm px-2 py-1"
          style={{ borderColor: 'var(--negative)', color: 'var(--negative)' }}
          role="alert"
          data-testid="operator-command-error"
        >
          Request failed — the backend could not be reached. The command was not sent.
        </div>
      )}

      {mutation.data && !busy && <ResultLine view={mutation.data} />}
    </section>
  );
}
