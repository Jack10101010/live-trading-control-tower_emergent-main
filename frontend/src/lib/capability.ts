/**
 * M-FLAGS-1 — the single capability seam.
 *
 * The old model was a boolean: 17 hardcoded flags, 16 of them `true`, each
 * asserting a module was available. Nothing consulted the capability itself, so
 * by the end of the honesty programme the flags disagreed with the views they
 * gated — `versionHistory: true` for a page reporting "no strategy-package
 * registry exists".
 *
 * A boolean cannot express the difference between "switched off", "not built",
 * "should exist but nothing is reporting" and "we cannot tell". Collapsing those
 * into `false` loses the reason, and collapsing them into `true` invents one.
 */
import type { Capability, CapabilityState } from '@/lib/api';

export const KNOWN_STATES: readonly CapabilityState[] =
  ['available', 'disabled', 'unsupported', 'unavailable', 'unknown'];

/**
 * Whether a capability may be USED. Only `available` qualifies.
 *
 * `unknown` fails closed: a state this client cannot classify is treated as
 * untrusted rather than waved through, exactly as the provenance gate treats an
 * unrecognised provenance.
 */
export function isUsable(cap: Capability | undefined | null): boolean {
  if (!cap || typeof cap.state !== 'string') return false;      // fail closed
  return cap.state === 'available';
}

/** Normalise anything unrecognised to `unknown` rather than guessing. */
export function stateOf(cap: Capability | undefined | null): CapabilityState {
  if (!cap || typeof cap.state !== 'string') return 'unknown';
  return (KNOWN_STATES as readonly string[]).includes(cap.state)
    ? (cap.state as CapabilityState)
    : 'unknown';
}

/** Operator-facing reason. Never says "disabled" for something never built. */
export function reasonFor(cap: Capability | undefined | null): string {
  const state = stateOf(cap);
  if (state === 'unknown') {
    return 'This capability could not be classified, so it is treated as unavailable.';
  }
  return cap?.detail || 'No further detail is reported.';
}

/** Title text per state — the four non-available states stay distinct. */
export function titleFor(state: CapabilityState): string {
  switch (state) {
    case 'disabled': return 'Capability switched off';
    case 'unsupported': return 'Not available in this build';
    case 'unavailable': return 'Capability unavailable';
    case 'unknown': return 'Capability state unknown';
    default: return 'Available';
  }
}
