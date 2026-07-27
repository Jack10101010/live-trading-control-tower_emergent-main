/**
 * LIVE-4E — the Recommendation detail view and operator decision surface.
 *
 * This is the ONLY place in the UI that can change a Recommendation, and the
 * only thing it can change is the recorded decision:
 *
 *   - ACCEPTING RECORDS A DECISION. It does not submit, modify or execute any
 *     trade. The notice saying so is permanent and unconditional — it renders
 *     whether or not the proposal is decidable, and it is not dismissible.
 *   - The controls are driven ENTIRELY by the backend's `decidable` /
 *     `undecidableReason`. The browser never re-derives lifecycle legality; a
 *     disabled button always states the backend's reason verbatim.
 *   - Every write carries the `version` the operator actually read, so a
 *     proposal that moved underneath them is a visible conflict, never a silent
 *     overwrite.
 *   - There is no authorization logic here. The operator id is asserted and the
 *     server decides; the view only reports how much that identity was worth.
 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  RecommendationDecisionError,
  type RecommendationDecisionView,
  type RecommendationEventView,
  type RecommendationOperationalView,
} from '@/lib/api';

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  ok: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  warn: { borderColor: 'var(--caution, var(--warning))', color: 'var(--caution, var(--warning))' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

/** The one sentence this whole slice exists to make true. */
export const DECISION_NOTICE =
  'Accepting a Recommendation records an operator decision only. It does not ' +
  'submit, modify, or execute any trade.';

/** Plain-English rendering of a backend refusal code. Never invented locally. */
const REASON_TEXT: Record<string, string> = {
  not_yet_proposed: 'This proposal has not been put forward for decision yet.',
  already_accepted: 'A decision was already recorded: accepted.',
  version_conflict: 'Another operator decided first. Refresh and review the current state.',
  decision_sequence_conflict: 'A concurrent decision won this slot. Refresh and review.',
  idempotency_key_reused: 'That request id already recorded a different decision.',
  reason_required: 'A reason is required.',
  operator_identity_required: 'No operator identity was supplied, so nothing was recorded.',
  recommendation_not_found: 'This recommendation no longer exists.',
};

function explain(code: string | null | undefined): string {
  if (!code) return '';
  if (REASON_TEXT[code]) return REASON_TEXT[code];
  if (code.startsWith('already_terminal:')) {
    return `This proposal is closed (${code.split(':')[1]}). Decisions are final.`;
  }
  if (code.startsWith('execution_in_progress:')) {
    return `Execution has already begun (${code.split(':')[1]}); it is no longer a proposal.`;
  }
  return code;
}

function txt(v: string | number | null | undefined): string {
  return v === null || v === undefined || v === '' ? '—' : String(v);
}
function price(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(5);
}
function num(v: number | null | undefined, digits = 2, suffix = ''): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(digits)}${suffix}`;
}

function Row({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs" data-testid={testId}>
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

/* ── decision history, newest first ───────────────────────────────────────── */

function DecisionHistory({ decisions }: { decisions: RecommendationDecisionView[] }) {
  // Newest first. Sorted here for display order only — the sequence itself is
  // assigned by the append-only store and is never recomputed.
  const ordered = useMemo(
    () => [...decisions].sort((a, b) => b.sequence - a.sequence),
    [decisions]);
  return (
    <div data-testid="decision-history">
      <div className="text-2xs uppercase tracking-wider text-text-muted mb-0.5">
        Decision history (newest first)
      </div>
      {ordered.length === 0 ? (
        <div className="text-2xs text-text-muted">No decisions recorded.</div>
      ) : (
        <ul className="space-y-1">
          {ordered.map((d) => (
            <li key={d.decisionId} className="text-2xs mono text-text-2"
                data-testid="decision-history-entry">
              <div>
                <span>{txt(d.occurredAt)}</span>{' · '}
                <span>{d.decisionType}</span>{' · '}
                <span>{txt(d.actor)}</span>{' · '}
                <span>v{txt(d.againstVersion)}→{d.sequence}</span>
                <span className={`${BADGE} ml-1`}
                      style={d.identityAssurance === 'authenticated' ? C.ok : C.warn}>
                  {d.identityAssurance.toUpperCase()}
                </span>
              </div>
              {d.reason && <div className="text-text-muted">reason: {d.reason}</div>}
              {d.note && <div className="text-text-muted">note: {d.note}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ── the decision form ────────────────────────────────────────────────────── */

type Pending = { kind: 'accept' | 'reject'; note: string; reason: string } | null;

function DecisionControls({ item, actorId, onDecided }: {
  item: RecommendationOperationalView;
  actorId: string;
  onDecided: () => void;
}) {
  const [pending, setPending] = useState<Pending>(null);
  const [failure, setFailure] = useState<{ code: string; detail: string } | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  // One key per decision attempt, so a retry of the SAME attempt replays
  // instead of recording a second decision.
  const [attemptKey, setAttemptKey] = useState(() => `dec-${item.recommendationId}-1`);

  useEffect(() => { setPending(null); setFailure(null); setOutcome(null); },
            [item.recommendationId]);

  const mutation = useMutation({
    mutationFn: (p: NonNullable<Pending>) =>
      api.decideTradeRecommendation(item.recommendationId, p.kind, {
        reason: p.reason.trim(),
        note: p.note.trim() || undefined,
        expectedVersion: item.version,
      }, { actorId, idempotencyKey: attemptKey }),
    onSuccess: (res) => {
      setOutcome(`Recorded ${res.decision.decisionType}. Status is now ${res.status}. `
                 + 'No order was submitted.');
      setPending(null);
      setFailure(null);
      onDecided();
    },
    onError: (err: unknown) => {
      const e = err as RecommendationDecisionError;
      setFailure({ code: e.code ?? 'decision_failed', detail: e.detail ?? '' });
      // A conflict means the world moved: a fresh attempt is a NEW decision, so
      // it must not reuse the previous idempotency key.
      if (e.isConflict) {
        setAttemptKey(`dec-${item.recommendationId}-${Date.now()}`);
        onDecided();
      }
    },
  });

  if (!item.decidable) {
    return (
      <div className="space-y-1" data-testid="decision-controls-disabled">
        <div className="flex gap-2">
          <button type="button" disabled data-testid="accept-button"
                  className="text-2xs mono px-2 py-1 rounded-sm border opacity-40"
                  style={C.muted as CSSProperties}>Accept</button>
          <button type="button" disabled data-testid="reject-button"
                  className="text-2xs mono px-2 py-1 rounded-sm border opacity-40"
                  style={C.muted as CSSProperties}>Reject</button>
        </div>
        <div className="text-2xs" style={{ color: 'var(--text-muted)' }}
             data-testid="decision-disabled-reason">
          Decisions are closed: {explain(item.undecidableReason)}
        </div>
      </div>
    );
  }

  if (outcome) {
    return (
      <div className="text-2xs" style={C.ok as CSSProperties} data-testid="decision-complete">
        {outcome}
      </div>
    );
  }

  if (pending) {
    const needsReason = pending.kind === 'reject';
    const ready = !needsReason || pending.reason.trim().length > 0;
    return (
      <div className="space-y-1.5" data-testid="decision-confirm">
        <div className="text-2xs text-text-2">
          Confirm <strong>{pending.kind.toUpperCase()}</strong> of{' '}
          <span className="mono">{item.recommendationId.slice(0, 12)}</span>{' '}
          ({txt(item.instrument)} {txt(item.direction)}), recorded against version{' '}
          <span className="mono">{item.version}</span>.
        </div>
        <label className="block text-2xs text-text-muted">
          {needsReason ? 'Rejection reason (required)' : 'Reason (required)'}
          <input type="text" value={pending.reason} data-testid="decision-reason"
                 onChange={(e) => setPending({ ...pending, reason: e.target.value })}
                 className="w-full mono text-2xs px-1.5 py-1 rounded-sm border bg-transparent"
                 style={C.muted as CSSProperties} />
        </label>
        <label className="block text-2xs text-text-muted">
          Note (optional)
          <input type="text" value={pending.note} data-testid="decision-note"
                 onChange={(e) => setPending({ ...pending, note: e.target.value })}
                 className="w-full mono text-2xs px-1.5 py-1 rounded-sm border bg-transparent"
                 style={C.muted as CSSProperties} />
        </label>
        <div className="text-2xs" style={{ color: 'var(--text-muted)' }}>
          {DECISION_NOTICE}
        </div>
        <div className="flex gap-2">
          <button type="button" data-testid="decision-confirm-button"
                  disabled={!ready || mutation.isPending}
                  onClick={() => mutation.mutate(pending)}
                  className="text-2xs mono px-2 py-1 rounded-sm border"
                  style={(ready ? C.ok : C.muted) as CSSProperties}>
            {mutation.isPending ? 'Recording…' : `Confirm ${pending.kind}`}
          </button>
          <button type="button" data-testid="decision-cancel-button"
                  onClick={() => { setPending(null); setFailure(null); }}
                  className="text-2xs mono px-2 py-1 rounded-sm border"
                  style={C.muted as CSSProperties}>Cancel</button>
        </div>
        {failure && (
          <div className="text-2xs" style={{ color: 'var(--negative)' }}
               data-testid="decision-error">
            Not recorded — {explain(failure.code)}
            {failure.detail ? ` (${failure.detail})` : ''}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-1" data-testid="decision-controls">
      <div className="flex gap-2">
        <button type="button" data-testid="accept-button"
                onClick={() => setPending({ kind: 'accept', note: '', reason: '' })}
                className="text-2xs mono px-2 py-1 rounded-sm border"
                style={C.ok as CSSProperties}>Accept</button>
        <button type="button" data-testid="reject-button"
                onClick={() => setPending({ kind: 'reject', note: '', reason: '' })}
                className="text-2xs mono px-2 py-1 rounded-sm border"
                style={C.bad as CSSProperties}>Reject</button>
      </div>
      {failure && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }}
             data-testid="decision-error">
          Not recorded — {explain(failure.code)}
        </div>
      )}
    </div>
  );
}

/* ── the detail view ──────────────────────────────────────────────────────── */

export function RecommendationDetail({ item, actorId, onLineageSelect }: {
  item: RecommendationOperationalView;
  actorId?: string;
  onLineageSelect?: (kind: 'scenario' | 'intent' | 'trade', id: string) => void;
}) {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['trade-recommendation', item.recommendationId],
    queryFn: () => api.tradeRecommendation(item.recommendationId),
    retry: false,
  });

  const decisions: RecommendationDecisionView[] = data?.decisions ?? [];
  const history: RecommendationEventView[] = data?.history ?? [];
  const importWarning = item.warnings.find((w) => w.includes('import')) ?? null;

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['trade-recommendations'] });
    queryClient.invalidateQueries({
      queryKey: ['trade-recommendation', item.recommendationId] });
  };

  const lineage: Array<[string, string | null, 'scenario' | 'intent' | 'trade']> = [
    ['Scenario', item.scenarioId, 'scenario'],
    ...item.linkedIntentIds.map((id) =>
      ['Intent', id, 'intent'] as [string, string, 'intent']),
  ];

  return (
    <div className="space-y-2 pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}
         data-testid="recommendation-detail">
      {/* Unconditional and permanent — it renders in every state. */}
      <div className={BADGE} style={C.muted} data-testid="decision-notice">
        {DECISION_NOTICE}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1">
        <div className="space-y-1" data-testid="recommendation-identity">
          <div className="text-2xs uppercase tracking-wider text-text-muted">
            Identity &amp; status
          </div>
          <Row label="Recommendation" value={txt(item.recommendationId)} />
          <Row label="Source" value={txt(item.source)} />
          <Row label="Status" value={txt(item.status)} />
          <Row label="Outcome" value={txt(item.outcome)} />
          <Row label="Version" value={txt(item.version)} testId="detail-version" />
          <Row label="Created" value={txt(item.createdAt)} />
          <Row label="Expires" value={txt(item.expiresAt)} />
        </div>
        <div className="space-y-1" data-testid="recommendation-terms">
          <div className="text-2xs uppercase tracking-wider text-text-muted">
            Proposed terms
          </div>
          <Row label="Instrument" value={txt(item.instrument)} />
          <Row label="Direction" value={txt(item.direction)} />
          <Row label="Entry" value={price(item.proposedEntry)} />
          <Row label="Stop loss" value={price(item.stopLoss)} />
          <Row label="Take profit" value={price(item.takeProfit)} />
          <Row label="Quantity" value={num(item.quantity)} />
          <Row label="Risk" value={num(item.riskAmount)} />
          <Row label="Planned R" value={item.plannedR === null ? '—' : `${item.plannedR}R`} />
        </div>
      </div>

      {importWarning && (
        <div className="text-2xs" style={{ color: 'var(--caution, var(--warning))' }}
             data-testid="import-warning">
          ⚠ {importWarning}
        </div>
      )}

      <div data-testid="recommendation-lineage">
        <div className="text-2xs uppercase tracking-wider text-text-muted">Lineage</div>
        <div className="flex flex-wrap gap-1.5">
          {lineage.map(([label, id, kind]) => (
            id ? (
              <button key={`${kind}-${id}`} type="button"
                      data-testid={`lineage-${kind}`}
                      onClick={() => onLineageSelect?.(kind, id)}
                      disabled={!onLineageSelect}
                      className={`${BADGE} ${onLineageSelect ? 'underline' : 'opacity-60'}`}
                      style={C.muted as CSSProperties}>
                {label}: {id}
              </button>
            ) : null
          ))}
          {item.linkedIntentIds.length === 0 && (
            <span className={BADGE} style={C.muted} data-testid="lineage-intent-none">
              Intent: none linked
            </span>
          )}
          <span className={BADGE} style={C.muted} data-testid="lineage-account">
            Account: {txt(item.accountFingerprintMasked)}
          </span>
          <span className={BADGE} style={C.muted} data-testid="lineage-node">
            Node: {txt(item.nodeId)}
          </span>
          <span className={BADGE} style={C.muted} data-testid="lineage-trade-none">
            {/* The ledger link exists only once a trade closes against this
                proposal; the read model does not carry it yet. */}
            Trade: not yet available
          </span>
        </div>
      </div>

      {actorId ? (
        <DecisionControls item={item} actorId={actorId} onDecided={refresh} />
      ) : (
        <div className="text-2xs" style={{ color: 'var(--text-muted)' }}
             data-testid="decision-no-actor">
          No operator identity is configured, so no decision can be recorded.
        </div>
      )}

      <DecisionHistory decisions={decisions} />

      <div data-testid="recommendation-events">
        <div className="text-2xs uppercase tracking-wider text-text-muted">
          Lifecycle events
        </div>
        <ul className="space-y-0.5">
          {history.map((e) => (
            <li key={e.eventId} className="text-2xs mono text-text-2">
              #{e.sequence} {e.eventType} · {e.occurredAt}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
