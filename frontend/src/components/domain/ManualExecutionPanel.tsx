/**
 * LIVE-3 — the manual-execution management surface.
 *
 *   - execution-mode selector (observe / manual_live / halted), confirmed;
 *   - issue / revoke a scoped durable authorization grant;
 *   - per-entity manual operations: modify SL/TP, cancel pending order, close
 *     position — each behind its own confirmation, each disabled until its
 *     exact gates pass;
 *   - acknowledgement is displayed SEPARATELY from the reconciliation-confirmed
 *     result; no autonomous control, no bulk action, no pending-order creation.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, MarketOrderError, type EntityOperationResponse } from '@/lib/api';

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs">
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

type PendingAction =
  | { kind: 'mode'; mode: string }
  | { kind: 'modify'; ref: string; sl?: number; tp?: number }
  | { kind: 'cancel'; ref: string }
  | { kind: 'close'; ref: string }
  | null;

export function ManualExecutionPanel() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<PendingAction>(null);
  const [posRef, setPosRef] = useState('');
  const [sl, setSl] = useState('');
  const [tp, setTp] = useState('');
  const [ordRef, setOrdRef] = useState('');
  const [grantAccount, setGrantAccount] = useState('');
  const [grantScope, setGrantScope] = useState('');
  const [result, setResult] = useState<EntityOperationResponse | null>(null);
  const [denial, setDenial] = useState<string | null>(null);

  const { data: state } = useQuery({
    queryKey: ['execution-state'],
    queryFn: () => api.executionState(),
    refetchInterval: 10_000,
    retry: false,
  });
  const { data: modeView } = useQuery({
    queryKey: ['execution-mode'],
    queryFn: () => api.executionMode(),
    refetchInterval: 10_000,
    retry: false,
  });

  const gov = state?.governance;
  const mode = gov?.executionMode ?? modeView?.mode ?? 'observe';
  const auth = gov?.authorization;
  const broker = state?.broker;
  const canOperate = mode === 'manual_live' || mode === 'halted';

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['execution-state'] });
    queryClient.invalidateQueries({ queryKey: ['execution-mode'] });
  };
  const onDeny = (err: unknown) => {
    setResult(null);
    setDenial(err instanceof MarketOrderError
      ? `DENIED at ${err.stage}: ${err.code}` : String(err));
  };
  const onOk = (res: EntityOperationResponse) => { setResult(res); setDenial(null); refresh(); };

  const modeMut = useMutation({
    mutationFn: (m: string) => api.setExecutionMode(m, `operator selected ${m}`),
    onSuccess: () => { setDenial(null); refresh(); },
    onError: (e) => setDenial(`mode transition refused: ${String(e).slice(0, 120)}`),
    onSettled: () => setPending(null),
  });
  const modifyMut = useMutation({
    mutationFn: (a: { ref: string; sl?: number; tp?: number }) =>
      api.modifyProtection(a.ref, { stopLoss: a.sl, takeProfit: a.tp }),
    onSuccess: onOk, onError: onDeny, onSettled: () => setPending(null),
  });
  const cancelMut = useMutation({
    mutationFn: (ref: string) => api.cancelPendingOrder(ref),
    onSuccess: onOk, onError: onDeny, onSettled: () => setPending(null),
  });
  const closeMut = useMutation({
    mutationFn: (ref: string) => api.closePosition(ref),
    onSuccess: onOk, onError: onDeny, onSettled: () => setPending(null),
  });
  const issueMut = useMutation({
    mutationFn: () => api.issueGrant({
      accountScope: grantAccount, scopes: grantScope ? [grantScope] : [],
      ttlSeconds: 3600, reason: 'manual session grant',
    }),
    onSuccess: () => { setDenial(null); refresh(); },
    onError: (e) => setDenial(`grant refused: ${String(e).slice(0, 120)}`),
  });

  const busy = modeMut.isPending || modifyMut.isPending || cancelMut.isPending || closeMut.isPending;
  const confirmAction = () => {
    if (!pending) return;
    if (pending.kind === 'mode') modeMut.mutate(pending.mode);
    else if (pending.kind === 'modify') modifyMut.mutate({ ref: pending.ref, sl: pending.sl, tp: pending.tp });
    else if (pending.kind === 'cancel') cancelMut.mutate(pending.ref);
    else if (pending.kind === 'close') closeMut.mutate(pending.ref);
  };

  const btn = 'text-2xs mono px-2 py-1 rounded-sm border disabled:opacity-40 disabled:cursor-not-allowed';
  const inp = 'text-2xs mono px-1.5 py-1 rounded-sm border bg-transparent w-full';
  const bStyle = { borderColor: 'var(--negative)', color: 'var(--negative)' } as const;
  const mStyle = { borderColor: 'var(--text-muted)', color: 'var(--text-muted)' } as const;

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Manual execution management"
      data-testid="manual-execution-panel"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Manual execution</h3>
        <span className="text-2xs mono px-1.5 py-0.5 rounded-sm border" style={bStyle}
              data-testid="manual-execution-scope">
          LIVE MANUAL · NO AUTOMATION · LOCAL LOOPBACK ONLY
        </span>
      </header>

      <div className="space-y-1.5">
        <Row label="Adapter" value={broker?.kind ?? '—'} />
        <Row label="Account" value={broker?.account?.login_masked ?? '— (masked when read)'} />
        <Row label="Execution mode" value={mode} />
        <Row label="Authorization"
             value={auth?.active ? `active — expires ${auth.expiresAt ?? '?'}` : 'none active'} />

        {/* mode selector */}
        <div className="flex gap-2 pt-1" data-testid="mode-selector">
          {(['observe', 'manual_live', 'halted'] as const).map((m) => (
            <button key={m} type="button" className={`flex-1 ${btn}`}
                    style={m === mode ? bStyle : mStyle}
                    disabled={busy || m === mode
                              || (m === 'manual_live'
                                  && !Object.values(modeView?.manualLiveGates ?? {}).every(Boolean))}
                    onClick={() => setPending({ kind: 'mode', mode: m })}
                    data-testid={`mode-${m}`}>
              {m}
            </button>
          ))}
        </div>
        {modeView && !Object.values(modeView.manualLiveGates ?? {}).every(Boolean) && (
          <div className="text-2xs text-text-muted" data-testid="manual-live-blocked">
            manual_live blocked by:{' '}
            {Object.entries(modeView.manualLiveGates).filter(([, ok]) => !ok).map(([k]) => k).join(', ')}
          </div>
        )}

        {/* grant issuance */}
        <div className="pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex gap-2">
            <input className={inp} style={mStyle} placeholder="account fingerprint scope"
                   value={grantAccount} onChange={(e) => setGrantAccount(e.target.value)}
                   data-testid="grant-account-scope" />
            <input className={inp} style={mStyle} placeholder="deployment scope"
                   value={grantScope} onChange={(e) => setGrantScope(e.target.value)}
                   data-testid="grant-deployment-scope" />
            <button type="button" className={btn} style={mStyle}
                    disabled={issueMut.isPending || !grantAccount || !grantScope}
                    onClick={() => issueMut.mutate()} data-testid="grant-issue">
              Issue grant
            </button>
          </div>
        </div>

        {/* entity operations */}
        <div className="pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex gap-2">
            <input className={inp} style={mStyle} placeholder="position ref"
                   value={posRef} onChange={(e) => setPosRef(e.target.value)}
                   data-testid="op-position-ref" />
            <input className={inp} style={mStyle} placeholder="new SL"
                   value={sl} onChange={(e) => setSl(e.target.value)} data-testid="op-sl" />
            <input className={inp} style={mStyle} placeholder="new TP"
                   value={tp} onChange={(e) => setTp(e.target.value)} data-testid="op-tp" />
          </div>
          <div className="flex gap-2">
            <button type="button" className={`flex-1 ${btn}`} style={bStyle}
                    disabled={busy || !canOperate || !posRef || (!sl && !tp) || mode === 'halted'}
                    onClick={() => setPending({ kind: 'modify', ref: posRef,
                                                sl: sl ? Number(sl) : undefined,
                                                tp: tp ? Number(tp) : undefined })}
                    data-testid="op-modify">
              Modify SL/TP
            </button>
            <button type="button" className={`flex-1 ${btn}`} style={bStyle}
                    disabled={busy || !canOperate || !posRef}
                    onClick={() => setPending({ kind: 'close', ref: posRef })}
                    data-testid="op-close">
              Close position
            </button>
          </div>
          <div className="flex gap-2">
            <input className={inp} style={mStyle} placeholder="pending order ref"
                   value={ordRef} onChange={(e) => setOrdRef(e.target.value)}
                   data-testid="op-order-ref" />
            <button type="button" className={btn} style={bStyle}
                    disabled={busy || !canOperate || !ordRef}
                    onClick={() => setPending({ kind: 'cancel', ref: ordRef })}
                    data-testid="op-cancel">
              Cancel order
            </button>
          </div>
        </div>

        {/* confirmation */}
        {pending && (
          <div className="space-y-1.5" data-testid="manual-confirm-dialog" role="alertdialog">
            <div className="text-2xs" style={{ color: 'var(--negative)' }}>
              CONFIRM: {pending.kind === 'mode'
                ? `switch execution mode to ${pending.mode}`
                : pending.kind === 'modify'
                  ? `modify protection on ${pending.ref}`
                  : pending.kind === 'cancel'
                    ? `cancel pending order ${pending.ref}`
                    : `close position ${pending.ref} in full`}?
              This is a live manual action; nothing else is executed.
            </div>
            <div className="flex gap-2">
              <button type="button" className={`flex-1 ${btn}`} style={bStyle}
                      disabled={busy} onClick={confirmAction} data-testid="manual-confirm">
                Confirm
              </button>
              <button type="button" className={`flex-1 ${btn}`} style={mStyle}
                      disabled={busy} onClick={() => setPending(null)}
                      data-testid="manual-cancel">
                Cancel
              </button>
            </div>
          </div>
        )}

        {denial && (
          <div className="text-2xs" style={{ color: 'var(--negative)' }} data-testid="manual-denial">
            {denial}
          </div>
        )}
        {result && (
          <div className="pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }}
               data-testid="manual-result">
            <Row label="Lifecycle" value={result.lifecycleState ?? 'unrecorded'} />
            <Row label="Acknowledged"
                 value={String((result.acknowledgement as { status?: string } | null)?.status ?? '—')} />
            <Row label="Reconciliation confirmed"
                 value={result.reconciliationConfirmed ? 'YES' : 'NOT YET — awaiting reconciliation'} />
            <Row label="Latency" value={result.latencyMs != null ? `${result.latencyMs} ms` : '—'} />
          </div>
        )}

        {gov?.operations?.available && (
          <div className="pt-1 border-t space-y-1" style={{ borderColor: 'var(--border-subtle)' }}
               data-testid="manual-operations-telemetry">
            <Row label="Awaiting confirmation"
                 value={String(gov.operations.awaitingAcknowledgementConfirmation?.length ?? 0)} />
            <Row label="Reconciliation required"
                 value={String(gov.operations.reconciliationRequired?.length ?? 0)} />
            <Row label="Confirmed" value={String(gov.operations.confirmed ?? 0)} />
            <Row label="Entity locks" value={String(gov.operations.entityLocks?.length ?? 0)} />
          </div>
        )}
      </div>

      <p className="text-2xs text-text-muted mt-2">
        Manual, individually confirmed operations only: modify SL/TP (never risk-increasing),
        cancel a pending order, close a position in full. No automation, no bulk actions, no
        pending-order creation. Completion is reconciliation-confirmed, not assumed from the
        broker acknowledgement.
      </p>
    </section>
  );
}
