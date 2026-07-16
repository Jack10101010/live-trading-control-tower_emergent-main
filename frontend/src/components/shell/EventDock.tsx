import { useEvents } from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import { IconButton } from '@/components/primitives/Button';
import { TimestampUTC } from '@/components/primitives';
import { ChevronDown, ChevronUp, Zap, GitBranch, TrendingUp, Cog } from 'lucide-react';
import { cn } from '@/lib/utils';

/**
 * EventDock — bottom band, live audit stream, filtered to scope (§C row 7).
 * Distinct from technical logs which live inside the Logs tab.
 */
export function EventDock() {
  const open = useShellStore((s) => s.eventDockOpen);
  const setOpen = useShellStore((s) => s.setEventDockOpen);
  const events = useEvents();

  return (
    <div
      className="shrink-0 border-t"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated)' }}
    >
      <button
        onClick={() => setOpen(!open)}
        className="w-full h-8 px-4 flex items-center gap-2 border-b hover:bg-[color:var(--panel-2)] transition-colors"
        style={{ borderColor: 'var(--border-subtle)' }}
        data-testid="event-dock-toggle"
      >
        <Zap size={12} className="text-[color:var(--warning)]" />
        <span className="text-[10px] uppercase tracking-widest font-medium text-text-2">
          Event Dock
        </span>
        <span className="text-2xs text-text-muted mono">{events.length} events</span>
        <span className="ml-auto flex items-center gap-2 text-2xs text-text-muted">
          <span className="mono">audit stream · scope-filtered</span>
          {open ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
        </span>
      </button>
      {open && (
        <div className="h-32 overflow-auto" data-testid="event-dock-content">
          <ul className="divide-y" style={{ borderColor: 'var(--border-subtle)' }}>
            {events.map((ev) => {
              const icon =
                ev.category === 'policy' ? (
                  <GitBranch size={11} className="text-[color:var(--recommendation)]" />
                ) : ev.category === 'trade' ? (
                  <TrendingUp size={11} className="text-[color:var(--positive)]" />
                ) : (
                  <Cog size={11} className="text-text-muted" />
                );
              return (
                <li
                  key={ev.eventId}
                  className="flex items-center gap-3 px-4 py-1.5 hover:bg-[color:var(--panel-2)] transition-colors"
                >
                  <span className="mono text-2xs text-text-muted w-16 shrink-0 truncate">
                    #{ev.seq}
                  </span>
                  <span className="shrink-0">{icon}</span>
                  <span className="text-2xs text-text-2 uppercase tracking-wider w-16 shrink-0">
                    {ev.category}
                  </span>
                  <span className="mono text-2xs text-text w-40 shrink-0 truncate">{ev.code}</span>
                  <span className="text-xs text-text truncate flex-1">{ev.humanExplanation}</span>
                  <span className="text-2xs text-text-muted shrink-0">
                    <TimestampUTC iso={ev.at} />
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
