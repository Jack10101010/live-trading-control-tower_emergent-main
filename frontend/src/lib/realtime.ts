import type { QueryKey } from '@tanstack/react-query';
import { queryClient } from '@/lib/queryClient';
import { QK } from '@/lib/api';
import type { EventEntry } from '@/types/domain';

/**
 * Realtime read side (Phase 5). Incoming events (from the command response or the
 * long-poll delta channel) are applied here: the event-log cache is patched
 * directly, and only the affected entity roots are invalidated so React Query
 * refetches the authoritative server view (runtime aggregates recompute on the
 * backend — never client-derived). `/api/world` is never invalidated.
 *
 * `lastAppliedSeq` is the monotonic dedup cursor: an event is applied at most once
 * regardless of whether it arrives via the issuing client's own command response
 * or the shared delta stream.
 */

let lastAppliedSeq = 0;

export function getLastAppliedSeq(): number {
  return lastAppliedSeq;
}

/** Seed the cursor from the initial snapshot so a fresh connection doesn't
 *  re-process the historical event log. */
export function seedLastAppliedSeq(seq: number): void {
  if (seq > lastAppliedSeq) lastAppliedSeq = seq;
}

/** Hard-reset the cursor (used on stale-cursor resync after a backend log reset,
 *  where the new head seq is LOWER than what we'd already applied). */
export function resetLastAppliedSeq(seq: number): void {
  lastAppliedSeq = seq;
}

const ROOT_KEYS: Record<string, QueryKey> = {
  trades: QK.trades,
  fleet: QK.fleet,
  packages: QK.packages,
  runtimeHealth: QK.runtimeHealth,
};

/** Map an event to the entity roots its effect touches (mirror of
 *  `commandMeta.invalidates`, but driven by the event category so it works for
 *  events from ANY client). */
function affectedRoots(ev: EventEntry): string[] {
  switch (ev.category) {
    case 'trade':
    case 'order':
      return ['trades', 'fleet', 'runtimeHealth'];
    case 'risk':
      return ['fleet', 'trades', 'runtimeHealth'];
    case 'system':
      return ['fleet', 'runtimeHealth'];
    case 'policy':
      return ['packages', 'fleet', 'runtimeHealth'];
    default:
      return ['runtimeHealth'];
  }
}

/**
 * Apply a batch of events to the cache. Returns how many were newly applied
 * (0 if all were duplicates below the cursor).
 */
export function applyEvents(events: EventEntry[]): number {
  const fresh = events.filter((e) => (e.seq ?? 0) > lastAppliedSeq);
  if (fresh.length === 0) return 0;
  lastAppliedSeq = Math.max(lastAppliedSeq, ...fresh.map((e) => e.seq ?? 0));

  // Direct-patch the unscoped event-log cache (the Event Dock) from the delta —
  // no refetch. Dedupe by eventId; keep seq order.
  queryClient.setQueryData<EventEntry[]>(QK.events, (old) => {
    if (!old) return old;
    const seen = new Set(old.map((e) => e.eventId));
    const merged = [...old, ...fresh.filter((e) => !seen.has(e.eventId))];
    merged.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    return merged;
  });

  // Pair-scoped events variants (pair Activity Timeline) → refetch.
  void queryClient.invalidateQueries({
    predicate: (q) => q.queryKey[0] === 'events' && q.queryKey.length > 1,
  });

  // Targeted invalidation of affected derived entities only. Never the world.
  const roots = new Set<string>();
  for (const e of fresh) for (const r of affectedRoots(e)) roots.add(r);
  roots.forEach((r) => void queryClient.invalidateQueries({ queryKey: ROOT_KEYS[r] }));

  return fresh.length;
}
