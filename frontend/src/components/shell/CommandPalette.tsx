import { useEffect, useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useShellStore } from '@/store/shellStore';
import { useConfiguredInstruments } from '@/hooks/useRepository';
import { Search, ArrowRight, Command as CmdIcon } from 'lucide-react';

interface CommandItem {
  id: string;
  label: string;
  hint?: string;
  group: string;
  action: () => void;
  keywords?: string;
}

/**
 * CommandPalette — ⌘K global. All navigation and quick actions.
 */
export function CommandPalette() {
  const open = useShellStore((s) => s.paletteOpen);
  const setOpen = useShellStore((s) => s.setPaletteOpen);
  const setActivePair = useShellStore((s) => s.setActivePair);
  const setMatrixLens = useShellStore((s) => s.setMatrixLens);
  const navigate = useNavigate();
  // M-FLEET-2: pair choices come from the CONFIGURED instrument universe, not
  // from fixture deployment records. Deployment commands are gone — there is no
  // authoritative deployment to command, and offering one for an invented
  // record put a fabricated entity behind an action.
  const { symbols: pairs } = useConfiguredInstruments();
  const [query, setQuery] = useState('');

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setOpen(!open);
      }
      if (open && e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, setOpen]);

  useEffect(() => {
    if (open) setQuery('');
  }, [open]);

  const commands = useMemo<CommandItem[]>(() => {
    const nav: CommandItem[] = [
      { id: 'nav-fleet', label: 'Go to Fleet Overview', group: 'Navigate', action: () => navigate('/fleet') },
      { id: 'nav-edge', label: 'Go to Edge Monitor (Global)', group: 'Navigate', action: () => navigate('/edge-monitor') },
      { id: 'nav-broker', label: 'Go to Broker Health', group: 'Navigate', action: () => navigate('/broker-health') },
      { id: 'nav-accounts', label: 'Go to Accounts & Protection', group: 'Navigate', action: () => navigate('/accounts') },
      { id: 'nav-marketdata', label: 'Go to Market Data', group: 'Navigate', action: () => navigate('/market-data') },
      { id: 'nav-deployments', label: 'Go to Deployments', group: 'Navigate', action: () => navigate('/deployments') },
      { id: 'nav-packages', label: 'Go to Strategy Packages', group: 'Navigate', action: () => navigate('/strategy-packages') },
      { id: 'nav-system', label: 'Go to System', group: 'Navigate', action: () => navigate('/system') },
      { id: 'nav-settings', label: 'Go to Settings', group: 'Navigate', action: () => navigate('/settings') },
      { id: 'nav-vh', label: 'Go to Version History', group: 'Navigate', action: () => navigate('/version-history') },
      { id: 'nav-pc', label: 'Go to Package Comparison', group: 'Navigate', action: () => navigate('/package-comparison') },
    ];
    const pairTabs = [
      { path: 'dashboard', label: 'Dashboard' },
      { path: 'policy', label: 'Policy Engine' },
      { path: 'replay', label: 'Replay' },
      { path: 'orders', label: 'Orders' },
      { path: 'trades', label: 'Trades' },
      { path: 'analytics', label: 'Analytics' },
      { path: 'edge-monitor', label: 'Edge Monitor' },
      { path: 'strategy-health', label: 'Strategy Health' },
      { path: 'activity', label: 'Activity Timeline' },
    ];
    const pairCmds: CommandItem[] = pairs.flatMap((pair) =>
      pairTabs.map((t) => ({
        id: `pair-${pair}-${t.path}`,
        label: `Open ${pair} · ${t.label}`,
        group: 'Pair',
        action: () => {
          setActivePair(pair);
          navigate(`/pair/${pair}/${t.path}`);
        },
      }))
    );
    const lensCmds: CommandItem[] = (['eligibility', 'targets', 'risk', 'recommendations', 'validation'] as const).map((l) => ({
      id: `lens-${l}`,
      label: `Matrix lens · ${l}`,
      group: 'Policy Matrix',
      action: () => setMatrixLens(l),
    }));
    const deployCmds: CommandItem[] = [];   // M-FLEET-2: no authoritative deployments
    return [...nav, ...pairCmds, ...lensCmds, ...deployCmds];
  }, [pairs, navigate, setActivePair, setMatrixLens]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) =>
        c.label.toLowerCase().includes(q) ||
        (c.hint && c.hint.toLowerCase().includes(q)) ||
        (c.keywords && c.keywords.toLowerCase().includes(q))
    );
  }, [commands, query]);

  const groups = useMemo(() => {
    const map = new Map<string, CommandItem[]>();
    filtered.forEach((c) => {
      const list = map.get(c.group) ?? [];
      list.push(c);
      map.set(c.group, list);
    });
    return Array.from(map.entries());
  }, [filtered]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[600] flex items-start justify-center pt-32"
      onClick={() => setOpen(false)}
      data-testid="command-palette"
    >
      <div className="absolute inset-0 bg-black/60" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-xl rounded-lg border ct-elev-3 overflow-hidden"
        style={{ background: 'var(--panel)', borderColor: 'var(--border)' }}
      >
        <div className="flex items-center gap-2 px-3 h-11 border-b" style={{ borderColor: 'var(--border-subtle)' }}>
          <Search size={14} className="text-text-muted" />
          <input
            autoFocus
            placeholder="Type a command, workspace, or pair…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1 bg-transparent border-0 outline-none text-sm text-text placeholder:text-text-muted"
            data-testid="command-palette-input"
          />
          <span className="text-2xs text-text-muted mono">ESC</span>
        </div>
        <div className="max-h-96 overflow-auto">
          {groups.length === 0 && (
            <div className="p-8 text-center text-text-muted text-sm">No commands match.</div>
          )}
          {groups.map(([groupName, items]) => (
            <div key={groupName}>
              <div className="px-3 py-1.5 text-[9px] uppercase tracking-widest text-text-muted border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                {groupName}
              </div>
              {items.map((c) => (
                <button
                  key={c.id}
                  onClick={() => {
                    c.action();
                    setOpen(false);
                  }}
                  className="w-full flex items-center gap-3 px-3 h-9 text-left text-sm hover:bg-[color:var(--panel-2)] group"
                >
                  <CmdIcon size={12} className="text-text-muted" />
                  <span className="text-text">{c.label}</span>
                  {c.hint && <span className="text-2xs text-text-muted truncate ml-2 flex-1">{c.hint}</span>}
                  <ArrowRight size={12} className="ml-auto text-text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
