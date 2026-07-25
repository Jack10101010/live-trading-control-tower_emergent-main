import { useSystemConfidence, useOperator, useFleet, useBackendHealth } from '@/hooks/useRepository';
import { HealthDot } from '@/components/primitives';
import { IconButton } from '@/components/primitives/Button';
import { useShellStore } from '@/store/shellStore';
import { Command, ShieldAlert, Radio, Sun, Moon, Monitor } from 'lucide-react';
import { useCommand } from '@/hooks/useCommand';
import type { ThemeName } from '@/store/shellStore';
import { deriveDataSourceBadge } from '@/lib/dataSource';
import { summarize, type Tone } from '@/lib/connectionState';
import { useConnectionState } from '@/components/domain/ConnectionPanel';

const CONNECTION_TONE_COLOR: Record<Tone, string> = {
  positive: 'var(--positive)',
  caution: 'var(--caution, var(--warning))',
  negative: 'var(--negative)',
  neutral: 'var(--text-muted)',
};

/**
 * CommandSafetyBar — top chrome. Global kill · mode banner · health · clock · ⌘K.
 * Never removed; visible on every workspace (§C).
 */
export function CommandSafetyBar() {
  const confidence = useSystemConfidence();
  const operator = useOperator();
  const { asOf, deployments } = useFleet();
  const setPaletteOpen = useShellStore((s) => s.setPaletteOpen);
  const theme = useShellStore((s) => s.theme);
  const setTheme = useShellStore((s) => s.setTheme);
  const realtime = useShellStore((s) => s.realtime);
  const dispatch = useCommand();
  const { health, failed: healthFailed } = useBackendHealth();
  // UI-1 — four independent dimensions, summarized without collapsing them into
  // one "connected" word. Never claims node or bridge health the backend has not
  // actually observed.
  const { connection, backend: backendState } = useConnectionState();
  const connectionSummary = summarize(backendState, connection);
  const connectionHint = connection
    ? `Backend ${backendState} · telemetry ${connection.telemetry} · node ${connection.node} · MT5 bridge ${connection.bridge}. Freshness threshold ${connection.staleAfterSeconds}s.`
    : `Backend ${backendState} · node telemetry not yet observed.`;

  // UI-0 — global data-source truth (pure, unit-tested in lib/dataSource.ts).
  const dataSource = deriveDataSourceBadge(health, healthFailed, connection?.telemetry);
  const sourceProvenance = dataSource.provenance;
  const sourceLabel = dataSource.label;
  const sourceHint = dataSource.hint;
  const sourceColor =
    sourceProvenance === 'live-node'
      ? 'var(--positive)'
      : sourceProvenance === 'unavailable'
        ? 'var(--negative)'
        : sourceProvenance === 'placeholder'
          ? 'var(--text-muted)'
          : 'var(--caution)';

  // Realtime read-side connection status (Phase 5).
  const rtColor =
    realtime.state === 'live'
      ? 'var(--positive)'
      : realtime.state === 'offline'
      ? 'var(--negative)'
      : 'var(--warning)';
  // UI-1: this chip describes ONE thing — this browser's event stream to the
  // Control Tower backend. It previously rendered a green "LIVE", which beside the
  // execution-mode banner read as "the trading system is live". It has never known
  // anything about the execution node or MT5; the connection chip below does.
  const rtLabel =
    realtime.state === 'live'
      ? 'stream ok'
      : realtime.state === 'connecting'
        ? 'stream connecting'
        : realtime.state === 'offline'
          ? 'stream offline'
          : 'stream retrying';

  const bandColor =
    confidence.band === 'healthy'
      ? 'var(--positive)'
      : confidence.band === 'critical'
      ? 'var(--negative)'
      : 'var(--warning)';
  const bandDot: 'ok' | 'warn' | 'critical' =
    confidence.band === 'healthy' ? 'ok' : confidence.band === 'critical' ? 'critical' : 'warn';

  // UI-3: this is the FIXTURE world's planned execution posture, NOT the live
  // node's runtime mode. The node's real mode is published in telemetry and shown
  // by the live-operations strip; leaving this unlabelled created two competing
  // "live status" surfaces with different meanings.
  // Execution posture — highest-risk active mode across the fleet (never hardcoded).
  const execMode: 'live' | 'demo' | 'mock' = deployments.some(
    (d) => d.executionMode === 'live' && d.liveEnabled
  )
    ? 'live'
    : deployments.some((d) => d.executionMode === 'demo')
    ? 'demo'
    : 'mock';
  const modeColor =
    execMode === 'live'
      ? 'var(--mode-live)'
      : execMode === 'demo'
      ? 'var(--mode-demo)'
      : 'var(--mode-mock)';

  const nextTheme: Record<ThemeName, ThemeName> = {
    'console-dark': 'midnight-navy',
    'midnight-navy': 'premium-light',
    'premium-light': 'console-dark',
  };
  const themeIcon =
    theme === 'premium-light' ? <Sun size={14} /> : theme === 'midnight-navy' ? <Monitor size={14} /> : <Moon size={14} />;

  return (
    <div
      className="flex items-center h-11 border-b relative z-20 shrink-0"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated)' }}
    >
      {/* Wordmark */}
      <div
        className="flex items-center gap-2 px-4 h-full border-r"
        style={{ borderColor: 'var(--border-subtle)', minWidth: 240 }}
      >
        <div
          className="w-6 h-6 rounded-md flex items-center justify-center relative overflow-hidden"
          style={{
            background: 'linear-gradient(140deg, #4C82F7 0%, #2FB6C9 100%)',
          }}
        >
          <span className="text-[10px] font-bold text-white mono">CT</span>
          <span
            className="absolute right-0.5 bottom-0.5 text-[7px] text-white/70"
            aria-hidden
          >
            ◆
          </span>
        </div>
        <div className="flex flex-col leading-none">
          <span className="text-[13px] font-semibold text-text">Control Tower</span>
          <span className="text-[9px] uppercase tracking-widest text-text-muted mt-0.5">
            Live Trading Ops
          </span>
        </div>
      </div>

      {/* Mode banner */}
      <div
        className="flex items-center gap-2 px-3 h-full border-r shrink-0"
        style={{ borderColor: 'var(--border-subtle)' }}
      >
        <Radio
          size={12}
          /* UI-3: pulsing removed — animation must not be a primary safety signal,
             and this banner describes the fixture world, not the live node. */
          style={{ color: modeColor }}
        />
        <span
          className="text-[11px] font-semibold tracking-widest uppercase"
          style={{ color: modeColor }}
          title={`Fixture-world planned execution posture: ${execMode}. This is NOT the live node's runtime mode — see the live-operations strip.`}
        >
          {`plan ${execMode}`}
        </span>
        {/* UI-0: persistent, global data-source truth. Replaces a hardcoded
            application-version banner. The operator must never have to infer
            fixture mode from developer knowledge. */}
        <span
          className="text-[9px] uppercase tracking-widest mono px-1.5 py-0.5 rounded-sm"
          style={{ color: sourceColor, border: `1px solid ${sourceColor}` }}
          title={sourceHint}
          data-testid="data-source-badge"
          data-provenance={sourceProvenance}
        >
          {sourceLabel}
        </span>
        <span
          className="flex items-center gap-1 pl-2 ml-1 border-l"
          style={{ borderColor: 'var(--border-subtle)' }}
          title={`Browser → backend event stream: ${rtLabel} · seq ${realtime.lastSeq}. This is the UI's own transport — it reports nothing about the execution node or MT5.`}
          data-testid="realtime-status"
        >
          <span
            /* UI-3: no pulse. Animation must not be a safety signal, and this dot
               describes the UI's own transport, not the trading system. */
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: rtColor }}
          />
          <span className="text-[9px] uppercase tracking-widest mono" style={{ color: rtColor }}>
            {rtLabel}
          </span>
        </span>
        {/* UI-1 — node/bridge truth, always named separately from the UI transport. */}
        <span
          className="flex items-center gap-1 pl-2 ml-1 border-l"
          style={{ borderColor: 'var(--border-subtle)' }}
          title={connectionHint}
          data-testid="connection-status"
        >
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: CONNECTION_TONE_COLOR[connectionSummary.tone] }}
          />
          <span
            className="text-[9px] uppercase tracking-widest mono"
            style={{ color: CONNECTION_TONE_COLOR[connectionSummary.tone] }}
          >
            {connectionSummary.label}
          </span>
        </span>
      </div>

      {/* System confidence gauge */}
      <div
        className="flex items-center gap-3 px-4 h-full border-r flex-1 min-w-0"
        style={{ borderColor: 'var(--border-subtle)' }}
      >
        <div className="flex items-center gap-2 shrink-0">
          <div className="text-[10px] uppercase tracking-widest text-text-muted">
            System Confidence
          </div>
          <div
            className="flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 border tabular"
            style={{
              color: bandColor,
              borderColor: `${bandColor}55`,
              background: `color-mix(in srgb, ${bandColor} 10%, transparent)`,
            }}
          >
            <HealthDot state={bandDot} size="sm" />
            <span className="text-sm font-semibold">{confidence.score}</span>
            <span className="text-2xs uppercase">{confidence.band}</span>
          </div>
        </div>
        <div className="text-xs text-text-2 truncate" title={confidence.explanation}>
          {confidence.explanation}
        </div>
      </div>

      {/* Global kill */}
      <button
        className="flex items-center gap-1.5 h-full px-3 text-xs font-semibold border-r hover:bg-[color:var(--panel-2)] transition-colors duration-fast"
        style={{ borderColor: 'var(--border-subtle)', color: 'var(--negative)' }}
        onClick={() => dispatch({ name: 'GlobalKill', payload: {} })}
        data-testid="global-kill-button"
      >
        <ShieldAlert size={14} />
        Global Kill
      </button>

      {/* Command palette trigger */}
      <button
        onClick={() => setPaletteOpen(true)}
        className="flex items-center gap-2 h-full px-3 text-xs text-text-2 border-r hover:bg-[color:var(--panel-2)] transition-colors duration-fast"
        style={{ borderColor: 'var(--border-subtle)' }}
        data-testid="command-palette-trigger"
      >
        <Command size={14} />
        <span>⌘K</span>
      </button>

      {/* Theme toggle */}
      <div className="px-2 h-full flex items-center border-r" style={{ borderColor: 'var(--border-subtle)' }}>
        <IconButton
          ariaLabel={`Theme: ${theme} — click to cycle`}
          onClick={() => setTheme(nextTheme[theme])}
          data-testid="theme-toggle"
        >
          {themeIcon}
        </IconButton>
      </div>

      {/* Operator */}
      <div className="flex items-center gap-2 px-3 h-full">
        <div className="w-6 h-6 rounded-full flex items-center justify-center text-2xs font-semibold" style={{ background: 'var(--panel-3)', color: 'var(--text)' }}>
          {String(operator.displayName).charAt(0)}
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-xs text-text">{String(operator.displayName)}</span>
          <span className="text-2xs text-text-muted mono">{new Date(asOf).toISOString().slice(11, 19)}Z</span>
        </div>
      </div>
    </div>
  );
}
