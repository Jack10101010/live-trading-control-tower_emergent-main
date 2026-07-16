import { useSystemConfidence, useOperator, useFleet } from '@/hooks/useRepository';
import { HealthDot } from '@/components/primitives';
import { IconButton } from '@/components/primitives/Button';
import { useShellStore } from '@/store/shellStore';
import { Command, ShieldAlert, Radio, Sun, Moon, Monitor } from 'lucide-react';
import { useCommand } from '@/hooks/useCommand';
import type { ThemeName } from '@/store/shellStore';

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

  // Realtime read-side connection status (Phase 5).
  const rtColor =
    realtime.state === 'live'
      ? 'var(--positive)'
      : realtime.state === 'offline'
      ? 'var(--negative)'
      : 'var(--warning)';
  const rtLabel =
    realtime.state === 'live' ? 'live' : realtime.state === 'connecting' ? 'connecting' : realtime.state === 'offline' ? 'offline' : 'reconnecting';

  const bandColor =
    confidence.band === 'healthy'
      ? 'var(--positive)'
      : confidence.band === 'critical'
      ? 'var(--negative)'
      : 'var(--warning)';
  const bandDot: 'ok' | 'warn' | 'critical' =
    confidence.band === 'healthy' ? 'ok' : confidence.band === 'critical' ? 'critical' : 'warn';

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
          className={execMode === 'live' ? 'ct-pulse-dot' : ''}
          style={{ color: modeColor }}
        />
        <span className="text-[11px] font-semibold tracking-widest uppercase" style={{ color: modeColor }}>
          {execMode}
        </span>
        <span className="text-[10px] text-text-muted mono">v1.0.0</span>
        <span
          className="flex items-center gap-1 pl-2 ml-1 border-l"
          style={{ borderColor: 'var(--border-subtle)' }}
          title={`Realtime: ${rtLabel} · seq ${realtime.lastSeq}`}
          data-testid="realtime-status"
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${realtime.state === 'live' ? 'ct-pulse-dot' : ''}`}
            style={{ background: rtColor }}
          />
          <span className="text-[9px] uppercase tracking-widest mono" style={{ color: rtColor }}>
            {rtLabel}
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
