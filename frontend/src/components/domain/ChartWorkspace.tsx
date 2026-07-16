import { useMemo, useState } from 'react';
import { ChartPanel } from '@/components/domain/ChartPanel';
import { type Candle } from '@/lib/chartData';
import {
  CHART_LAYERS,
  DEFAULT_LAYER_IDS,
  buildAnnotations,
  type LayerContext,
} from '@/lib/chartLayers';
import { useTrades, useMarketCandles, useOrderBlocks, useFairValueGaps, useLiquidityPools, useMarketStructure } from '@/hooks/useRepository';
import { cn } from '@/lib/utils';

/**
 * ChartWorkspace — the canonical chart surface of the Control Tower (Phase 15/16).
 *
 * It is the ONE place that: (1) sources candles from the MARKET DATA ENGINE via
 * `useMarketCandles` (the single candle pipeline — replay asks the ReplayProvider,
 * live asks the active provider; no frontend OHLC generation), (2) assembles the
 * shared `LayerContext` from the existing React-Query hooks (no duplicate state),
 * (3) composes the enabled `ChartLayer`s into one annotation bundle, and (4) renders
 * ONE `ChartPanel`.
 *
 * Replay and live use the SAME instance/path — the only difference is which provider
 * is asked (`mode`) and the `cursor`. Every future overlay is an additive
 * `ChartLayer`; this component never needs to change to gain one.
 */

export interface ChartWorkspaceProps {
  instrument: string;
  mode: 'live' | 'replay';
  /** Replay virtual time (unix seconds). Omit for live. */
  cursor?: number;
  /** Candle window end (replay session end, or now for live). */
  endISO?: string;
  count?: number;
  height?: number;
  className?: string;
  volume?: boolean;
  /** Show the layer-toggle bar (default true). */
  controls?: boolean;
}

export function ChartWorkspace({
  instrument,
  mode,
  cursor,
  endISO,
  count = 220,
  height = 0,
  className,
  volume = true,
  controls = true,
}: ChartWorkspaceProps) {
  // Single candle source: the Market Data Engine (Phase 16). Replay asks the
  // ReplayProvider; live asks the active provider (switch provider ⇒ chart follows,
  // no ChartWorkspace change). This is the ONLY candle pipeline in the Control Tower.
  const feed = useMarketCandles(instrument, {
    provider: mode === 'replay' ? 'replay' : undefined,
    count,
    endISO,
  });
  const candles: Candle[] = feed?.candles ?? [];

  // Shared data from the existing hooks (React Query dedupes — no duplicate fetch/state).
  const { live, ghost } = useTrades({ pair: instrument });
  // Order blocks (Phase 17) via the swappable provider — one line, populated once here.
  const orderBlocks = useOrderBlocks(instrument, candles);
  // Fair value gaps (Phase 18) — identical seam, one line.
  const fairValueGaps = useFairValueGaps(instrument, candles);
  // Liquidity pools (Phase 19) — identical seam, one line.
  const liquidityPools = useLiquidityPools(instrument, candles);
  // Market structure — swings + BOS/CHOCH (Phase 20) — ONE provider, TWO layers.
  const marketStructure = useMarketStructure(instrument, candles);

  // Layer visibility — local workspace state seeded from the registry defaults.
  const [enabled, setEnabled] = useState<Set<string>>(() => new Set(DEFAULT_LAYER_IDS));
  const toggle = (id: string) =>
    setEnabled((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const nowISO = cursor != null ? new Date(cursor * 1000).toISOString() : endISO ?? new Date().toISOString();

  const ctx: LayerContext = useMemo(
    () => ({ instrument, candles, nowISO, cursor, mode, trades: live, ghosts: ghost, snapshot: null, orderBlocks, fairValueGaps, liquidityPools, marketStructure }),
    [instrument, candles, nowISO, cursor, mode, live, ghost, orderBlocks, fairValueGaps, liquidityPools, marketStructure]
  );

  const { markers, zones, priceLines } = useMemo(() => buildAnnotations(enabled, ctx), [enabled, ctx]);

  return (
    <div className={cn('flex flex-col min-h-0 h-full', className)}>
      {controls && (
        <div
          className="flex items-center gap-1.5 px-3 h-8 shrink-0 border-b overflow-x-auto"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
          data-testid="chart-layer-bar"
        >
          <span className="text-2xs text-text-muted uppercase tracking-wide mr-1 shrink-0">Layers</span>
          {CHART_LAYERS.map((l) => {
            const on = enabled.has(l.id);
            return (
              <button
                key={l.id}
                onClick={() => toggle(l.id)}
                data-testid={`layer-toggle-${l.id}`}
                aria-pressed={on}
                className="h-5 px-2 text-2xs rounded-sm shrink-0 transition-colors"
                style={{
                  color: on ? 'var(--text)' : 'var(--text-muted)',
                  background: on ? 'var(--panel-2)' : 'transparent',
                  border: `1px solid ${on ? 'var(--border)' : 'transparent'}`,
                }}
              >
                {l.label}
              </button>
            );
          })}
        </div>
      )}

      <div className="flex-1 min-h-0 relative">
        <div className="absolute inset-0">
          {candles.length > 0 ? (
            <ChartPanel
              instrument={instrument}
              candles={candles}
              height={height}
              className="h-full w-full"
              volume={volume}
              sessionShading={false}
              markers={markers}
              zones={zones}
              priceLines={priceLines}
              cursorTime={cursor}
            />
          ) : (
            <div
              className="h-full w-full flex flex-col items-center justify-center gap-2.5"
              data-testid="chart-feed-loading"
            >
              {feed === undefined ? (
                <>
                  <div
                    className="w-5 h-5 rounded-full animate-spin"
                    style={{
                      border: '2px solid color-mix(in srgb, var(--primary) 22%, transparent)',
                      borderTopColor: 'var(--primary)',
                    }}
                  />
                  <span className="text-2xs text-text-muted mono tracking-wide">Loading market feed…</span>
                </>
              ) : (
                <>
                  <div
                    className="flex items-end gap-[3px] h-5 opacity-40"
                    aria-hidden
                  >
                    {[9, 15, 7, 12, 5].map((h, i) => (
                      <span key={i} className="w-[3px] rounded-sm" style={{ height: h, background: 'var(--text-muted)' }} />
                    ))}
                  </div>
                  <span className="text-xs text-text-2 mono">No candles from feed</span>
                  <span className="text-2xs text-text-muted">the active market-data provider returned no bars</span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
