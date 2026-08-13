/**
 * LIVE-2 — the minimal execution surface. Exactly ONE control:
 *
 *     Submit Market Order
 *
 *   - DISABLED unless every backend readiness gate passes (derived truth —
 *     the button renders the conjunction the backend reports, nothing local);
 *   - an explicit confirmation step before anything is sent;
 *   - unmistakably labelled LIVE EXECUTION / ONE MARKET ORDER / NO AUTOMATION;
 *   - shows the broker acknowledgement, ticket, lifecycle state, failures and
 *     measured latency after a submission;
 *   - NO other execution control exists here or anywhere else in the SPA.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, MarketOrderError, type MarketOrderResponse } from '@/lib/api';

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs">
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

const ORDER = { instrument: 'EURUSD', side: 'long' as const, quantity: 0.01 };

export function MarketOrderPanel() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<MarketOrderResponse | null>(null);
  const [denial, setDenial] = useState<MarketOrderError | null>(null);

  const { data } = useQuery({
    queryKey: ['execution-state'],
    queryFn: () => api.executionState(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });

  const gates = data?.readiness?.gates ?? {};
  const ready = data?.readiness?.tradingReady === true;
  const failingGates = Object.entries(gates)
    .filter(([, ok]) => !ok)
    .map(([name]) => name);
  const marketOrder = data?.marketOrder;

  const submit = useMutation({
    mutationFn: () => api.submitMarketOrder(ORDER),
    onSuccess: (res) => {
      setResult(res);
      setDenial(null);
      queryClient.invalidateQueries({ queryKey: ['execution-state'] });
    },
    onError: (err) => {
      setResult(null);
      setDenial(err instanceof MarketOrderError ? err : new MarketOrderError('error', 'transport', String(err)));
    },
    onSettled: () => setConfirming(false),
  });

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Market order (live execution)"
      data-testid="market-order-panel"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Market order</h3>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: 'var(--negative)', color: 'var(--negative)' }}
          data-testid="market-order-scope"
        >
          LIVE EXECUTION · ONE MARKET ORDER · NO AUTOMATION
        </span>
      </header>

      <div className="space-y-1.5">
        <Row label="Order" value={`${ORDER.side} ${ORDER.quantity} ${ORDER.instrument} (market)`} />
        <Row label="Execution ready" value={ready ? 'YES — all gates pass' : 'NO'} />
        {!ready && failingGates.length > 0 && (
          <div className="text-2xs text-text-muted" data-testid="market-order-failing-gates">
            Blocked by: {failingGates.join(', ')}
          </div>
        )}

        {!confirming ? (
          <button
            type="button"
            className="w-full text-2xs mono px-2 py-1.5 rounded-sm border disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ borderColor: 'var(--negative)', color: 'var(--negative)' }}
            disabled={!ready || submit.isPending}
            onClick={() => setConfirming(true)}
            data-testid="market-order-submit"
          >
            Submit Market Order
          </button>
        ) : (
          <div className="space-y-1.5" data-testid="market-order-confirm-dialog" role="alertdialog">
            <div className="text-2xs" style={{ color: 'var(--negative)' }}>
              CONFIRM LIVE EXECUTION: submit ONE market order — {ORDER.side} {ORDER.quantity}{' '}
              {ORDER.instrument}? This sends a real order to the active broker. No automation
              follows; nothing else is executed.
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                className="flex-1 text-2xs mono px-2 py-1.5 rounded-sm border"
                style={{ borderColor: 'var(--negative)', color: 'var(--negative)' }}
                disabled={submit.isPending}
                onClick={() => submit.mutate()}
                data-testid="market-order-confirm"
              >
                {submit.isPending ? 'Submitting…' : 'Confirm — submit one market order'}
              </button>
              <button
                type="button"
                className="flex-1 text-2xs mono px-2 py-1.5 rounded-sm border"
                style={{ borderColor: 'var(--text-muted)', color: 'var(--text-muted)' }}
                disabled={submit.isPending}
                onClick={() => setConfirming(false)}
                data-testid="market-order-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {denial && (
          <div className="text-2xs" style={{ color: 'var(--negative)' }} data-testid="market-order-denial">
            DENIED at {denial.stage}: {denial.code}
            {denial.reason ? ` — ${denial.reason}` : ''}
          </div>
        )}

        {result && (
          <div className="mt-1 pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }} data-testid="market-order-result">
            <Row label="Lifecycle" value={result.lifecycleState ?? 'unrecorded'} />
            <Row label="Broker ticket" value={result.brokerTicket ?? '—'} />
            <Row
              label="Acknowledgement"
              value={String((result.acknowledgement as { status?: string } | null)?.status ?? '—')}
            />
            <Row label="Latency" value={result.latencyMs != null ? `${result.latencyMs} ms` : '—'} />
            {result.deduplicated && (
              <div className="text-2xs text-text-muted" data-testid="market-order-deduplicated">
                Duplicate submission — the original order's durable outcome was returned; no second
                broker order was created.
              </div>
            )}
          </div>
        )}

        {marketOrder?.available && marketOrder.lastSubmission && (
          <div className="mt-1 pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }} data-testid="market-order-telemetry">
            <Row label="Active market orders" value={String(marketOrder.activeMarketOrders ?? 0)} />
            <Row label="Pending submissions" value={String(marketOrder.pendingSubmissions ?? 0)} />
            <Row label="Submission failures" value={String(marketOrder.submissionFailures ?? 0)} />
            <Row
              label="Last submission"
              value={`${marketOrder.lastSubmission.state ?? '—'}${
                marketOrder.lastSubmission.finalReason ? ` (${marketOrder.lastSubmission.finalReason})` : ''
              }`}
            />
          </div>
        )}
      </div>

      <p className="text-2xs text-text-muted mt-2">
        This is the only execution control in the Control Tower. It submits exactly one market
        order through the canonical pipeline (authentication → authorization → safety → broker),
        and is disabled unless every readiness gate passes. All other broker mutations remain
        structurally unavailable.
      </p>
    </section>
  );
}
