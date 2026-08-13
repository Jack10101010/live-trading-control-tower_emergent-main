/**
 * LIVE-4E — the operator decision surface in the browser.
 *
 * Proves: the accept/reject flow (review → reason → optional note → confirm →
 * complete), controls disabled with the backend's own explanation, the
 * permanent "acceptance is not execution" notice, newest-first history, version
 * threading and visible conflicts, and that the view performs no authorization
 * or lifecycle reasoning of its own.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  api,
  RecommendationDecisionError,
  type RecommendationDecisionResult,
  type RecommendationDecisionView,
  type RecommendationOperationalView,
} from '@/lib/api';
import {
  DECISION_NOTICE,
  RecommendationDetail,
} from '@/components/domain/RecommendationDetail';

function decision(
  over: Partial<RecommendationDecisionView> = {},
): RecommendationDecisionView {
  return {
    decisionId: 'dec_1', decisionType: 'ACCEPT', actorType: 'OPERATOR',
    actor: 'actor_9f2a1b7c4d5e', occurredAt: '2026-07-27T12:05:00Z', sequence: 3,
    reason: 'conditions met', authorizationReference: null,
    executionMode: 'observe', note: 'clean BOS', againstVersion: 2,
    correlationId: 'req_1', identityAssurance: 'asserted', ...over,
  };
}

function item(
  over: Partial<RecommendationOperationalView> = {},
): RecommendationOperationalView {
  return {
    recommendationId: 'rcm_aaaaaaaaaaaaaaaa', scenarioId: 'scn_bbbbbbbbbbbbbbbb',
    instrument: 'EURUSD', direction: 'long', source: 'OPERATOR',
    status: 'PROPOSED', outcome: 'NOT_EXECUTED', latestDecision: null,
    actorType: null, proposedEntry: 1.1, stopLoss: 1.095, takeProfit: 1.11,
    quantity: 0.5, riskAmount: 25, riskPercent: 0.5, plannedR: 2,
    rationale: 'BOS retest', confidence: 0.72, linkedIntentCount: 0,
    linkedIntentIds: [], supersededBy: null, supersedes: null,
    createdAt: '2026-07-27T12:00:00Z', expiresAt: '2026-07-27T18:00:00Z',
    ageSeconds: 300, pastDue: false, active: true, nodeId: 'node-1',
    accountFingerprintMasked: 'acct…fp_1', executionTermsAvailability: 'ok',
    riskTermsAvailability: 'ok', version: 2, decidable: true,
    undecidableReason: null, warnings: [], tags: [],
    provenance: 'durable-store', freshness: null, ...over,
  };
}

function result(over: Partial<RecommendationDecisionResult> = {}):
    RecommendationDecisionResult {
  return {
    recorded: true, replayed: false, recommendationId: 'rcm_aaaaaaaaaaaaaaaa',
    status: 'ACCEPTED', version: 3, outcome: 'NOT_EXECUTED',
    decision: decision(), executed: false,
    notice: DECISION_NOTICE, ...over,
  };
}

function mount(over: Partial<RecommendationOperationalView> = {},
               props: { actorId?: string } = { actorId: 'op_jane' }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecommendationDetail item={item(over)} actorId={props.actorId} />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'tradeRecommendation').mockResolvedValue({
    ...item(), decisions: [], history: [],
  });
});
afterEach(() => cleanup());

describe('RecommendationDetail', () => {
  it('shows identity, terms and lineage', async () => {
    mount();
    await waitFor(() => screen.getByTestId('recommendation-detail'));
    const identity = screen.getByTestId('recommendation-identity').textContent ?? '';
    expect(identity).toContain('rcm_aaaaaaaaaaaaaaaa');
    expect(identity).toContain('PROPOSED');
    expect(identity).toContain('OPERATOR');
    const terms = screen.getByTestId('recommendation-terms').textContent ?? '';
    expect(terms).toContain('1.09500');
    expect(terms).toContain('2R');
    const lineage = screen.getByTestId('recommendation-lineage').textContent ?? '';
    expect(lineage).toContain('scn_bbbbbbbbbbbbbbbb');
    expect(lineage).toContain('acct…fp_1');
  });

  it('always states that acceptance is not execution', async () => {
    mount({ status: 'REJECTED', decidable: false,
            undecidableReason: 'already_terminal:REJECTED' });
    await waitFor(() => screen.getByTestId('decision-notice'));
    // The notice renders even when no decision is possible — it is permanent.
    expect(screen.getByTestId('decision-notice').textContent)
      .toContain('does not submit, modify, or execute any trade');
  });

  it('shows an import warning when the proposal came from fixture evidence', async () => {
    mount({ warnings: ['imported fixture evidence: a POLICY-CHANGE proposal, '
                       + 'not a trade proposal'] });
    await waitFor(() => screen.getByTestId('import-warning'));
    expect(screen.getByTestId('import-warning').textContent)
      .toContain('POLICY-CHANGE proposal');
  });

  it('disables the controls and states the backend reason verbatim', async () => {
    mount({ status: 'ACCEPTED', decidable: false,
            undecidableReason: 'already_accepted' });
    await waitFor(() => screen.getByTestId('decision-controls-disabled'));
    expect((screen.getByTestId('accept-button') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('reject-button') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('decision-disabled-reason').textContent)
      .toContain('A decision was already recorded: accepted.');
  });

  it('explains an execution-in-progress state without offering controls', async () => {
    mount({ status: 'INTENT_CREATED', decidable: false,
            undecidableReason: 'execution_in_progress:INTENT_CREATED' });
    await waitFor(() => screen.getByTestId('decision-disabled-reason'));
    expect(screen.getByTestId('decision-disabled-reason').textContent)
      .toContain('Execution has already begun');
    expect((screen.getByTestId('accept-button') as HTMLButtonElement).disabled).toBe(true);
  });

  it('records an acceptance through review, note and confirm', async () => {
    const spy = vi.spyOn(api, 'decideTradeRecommendation')
      .mockResolvedValue(result());
    mount();
    await waitFor(() => screen.getByTestId('decision-controls'));

    fireEvent.click(screen.getByTestId('accept-button'));
    await waitFor(() => screen.getByTestId('decision-confirm'));
    // The review step names what is being decided and against which version.
    expect(screen.getByTestId('decision-confirm').textContent).toContain('ACCEPT');
    expect(screen.getByTestId('decision-confirm').textContent).toContain('2');

    fireEvent.change(screen.getByTestId('decision-reason'),
                     { target: { value: 'conditions met' } });
    fireEvent.change(screen.getByTestId('decision-note'),
                     { target: { value: 'clean BOS' } });
    fireEvent.click(screen.getByTestId('decision-confirm-button'));

    await waitFor(() => screen.getByTestId('decision-complete'));
    expect(screen.getByTestId('decision-complete').textContent)
      .toContain('No order was submitted');
    expect(spy).toHaveBeenCalledTimes(1);
    const [, verb, body, opts] = spy.mock.calls[0];
    expect(verb).toBe('accept');
    expect(body).toMatchObject({ reason: 'conditions met', note: 'clean BOS',
                                 expectedVersion: 2 });
    expect(opts.actorId).toBe('op_jane');
    expect(opts.idempotencyKey).toBeTruthy();
  });

  it('requires a reason before a rejection can be confirmed', async () => {
    const spy = vi.spyOn(api, 'decideTradeRecommendation').mockResolvedValue(
      result({ status: 'REJECTED', decision: decision({ decisionType: 'REJECT' }) }));
    mount();
    await waitFor(() => screen.getByTestId('decision-controls'));
    fireEvent.click(screen.getByTestId('reject-button'));
    await waitFor(() => screen.getByTestId('decision-confirm'));

    expect((screen.getByTestId('decision-confirm-button') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId('decision-confirm-button'));
    expect(spy).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId('decision-reason'),
                     { target: { value: 'structure invalidated' } });
    expect((screen.getByTestId('decision-confirm-button') as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId('decision-confirm-button'));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy.mock.calls[0][1]).toBe('reject');
  });

  it('can be cancelled without recording anything', async () => {
    const spy = vi.spyOn(api, 'decideTradeRecommendation').mockResolvedValue(result());
    mount();
    await waitFor(() => screen.getByTestId('decision-controls'));
    fireEvent.click(screen.getByTestId('accept-button'));
    await waitFor(() => screen.getByTestId('decision-confirm'));
    fireEvent.click(screen.getByTestId('decision-cancel-button'));
    await waitFor(() => screen.getByTestId('decision-controls'));
    expect(spy).not.toHaveBeenCalled();
  });

  it('reports a concurrent decision as a conflict, not a success', async () => {
    vi.spyOn(api, 'decideTradeRecommendation').mockRejectedValue(
      new RecommendationDecisionError(
        { error: 'conflict', code: 'version_conflict', detail: '', executed: false,
          currentStatus: 'REJECTED', currentVersion: 3 }, 409));
    mount();
    await waitFor(() => screen.getByTestId('decision-controls'));
    fireEvent.click(screen.getByTestId('accept-button'));
    await waitFor(() => screen.getByTestId('decision-confirm'));
    fireEvent.change(screen.getByTestId('decision-reason'),
                     { target: { value: 'go' } });
    fireEvent.click(screen.getByTestId('decision-confirm-button'));

    await waitFor(() => screen.getByTestId('decision-error'));
    const error = screen.getByTestId('decision-error').textContent ?? '';
    expect(error).toContain('Not recorded');
    expect(error).toContain('Another operator decided first');
    // It never claims success.
    expect(screen.queryByTestId('decision-complete')).toBeNull();
  });

  it('surfaces a refusal without claiming anything executed', async () => {
    vi.spyOn(api, 'decideTradeRecommendation').mockRejectedValue(
      new RecommendationDecisionError(
        { error: 'rejected', code: 'reason_required', detail: '', executed: false },
        422));
    mount();
    await waitFor(() => screen.getByTestId('decision-controls'));
    fireEvent.click(screen.getByTestId('accept-button'));
    await waitFor(() => screen.getByTestId('decision-confirm'));
    fireEvent.change(screen.getByTestId('decision-reason'), { target: { value: 'x' } });
    fireEvent.click(screen.getByTestId('decision-confirm-button'));
    await waitFor(() => screen.getByTestId('decision-error'));
    expect(screen.getByTestId('decision-error').textContent)
      .toContain('A reason is required');
  });

  it('renders decision history newest first with actor, version and assurance',
     async () => {
    vi.spyOn(api, 'tradeRecommendation').mockResolvedValue({
      ...item(),
      decisions: [
        decision({ decisionId: 'dec_a', decisionType: 'DEFER', sequence: 2,
                   reason: 'awaiting open', note: null, againstVersion: 1 }),
        decision({ decisionId: 'dec_b', decisionType: 'ACCEPT', sequence: 3 }),
      ],
      history: [],
    });
    mount();
    await waitFor(() =>
      expect(screen.getAllByTestId('decision-history-entry')).toHaveLength(2));
    const entries = screen.getAllByTestId('decision-history-entry');
    expect(entries[0].textContent).toContain('ACCEPT');        // newest first
    expect(entries[1].textContent).toContain('DEFER');
    expect(entries[0].textContent).toContain('actor_9f2a1b7c4d5e');
    expect(entries[0].textContent).toContain('v2→3');
    expect(entries[0].textContent).toContain('ASSERTED');
    expect(entries[0].textContent).toContain('conditions met');
    expect(entries[0].textContent).toContain('clean BOS');
  });

  it('distinguishes an authenticated identity from an asserted one', async () => {
    vi.spyOn(api, 'tradeRecommendation').mockResolvedValue({
      ...item(),
      decisions: [decision({ identityAssurance: 'authenticated' })],
      history: [],
    });
    mount();
    await waitFor(() => expect(
      screen.getByTestId('decision-history').textContent).toContain('AUTHENTICATED'));
  });

  it('records nothing when no operator identity is configured', async () => {
    const spy = vi.spyOn(api, 'decideTradeRecommendation').mockResolvedValue(result());
    mount({}, { actorId: undefined });
    await waitFor(() => screen.getByTestId('decision-no-actor'));
    expect(screen.queryByTestId('accept-button')).toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it('performs no authorization or lifecycle reasoning of its own', async () => {
    // The view trusts `decidable` alone: a PROPOSED item marked undecidable by
    // the backend stays disabled, and a terminal-looking one marked decidable
    // stays enabled. The browser never re-derives legality.
    const closed = mount({ status: 'PROPOSED', decidable: false,
                           undecidableReason: 'not_decidable:PROPOSED' });
    await waitFor(() => screen.getByTestId('decision-controls-disabled'));
    expect((screen.getByTestId('accept-button') as HTMLButtonElement).disabled).toBe(true);
    closed.unmount();

    mount({ status: 'EXPIRED', decidable: true, undecidableReason: null });
    await waitFor(() => screen.getByTestId('decision-controls'));
    expect((screen.getByTestId('accept-button') as HTMLButtonElement).disabled).toBe(false);
  });
});
