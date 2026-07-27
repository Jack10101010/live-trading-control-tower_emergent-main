/**
 * ARCH-3 — the operator authentication session: memory-only, one header owner,
 * 401 distinct from offline, token never rendered or persisted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import {
  ApiAuthError,
  authHeader,
  clearOperatorToken,
  hasOperatorToken,
  isAuthError,
  setOperatorToken,
} from '@/lib/authSession';
import { deriveBackendState } from '@/lib/connectionState';
import { api } from '@/lib/api';
import { AuthSessionPanel } from '@/components/domain/AuthSessionPanel';

const TOKEN = 'op-token-' + 'x'.repeat(32);

beforeEach(() => {
  clearOperatorToken();
  vi.restoreAllMocks();
});
afterEach(() => {
  clearOperatorToken();
  cleanup();
  vi.unstubAllGlobals();
});

describe('session module (the single header owner)', () => {
  it('holds the token in memory only and clears explicitly', () => {
    expect(hasOperatorToken()).toBe(false);
    expect(authHeader()).toEqual({});
    setOperatorToken(TOKEN);
    expect(hasOperatorToken()).toBe(true);
    expect(authHeader()).toEqual({ Authorization: `Bearer ${TOKEN}` });
    clearOperatorToken();
    expect(authHeader()).toEqual({});
  });

  it('never touches browser storage', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    setOperatorToken(TOKEN);
    clearOperatorToken();
    expect(setItem).not.toHaveBeenCalled();
    expect(Object.keys(localStorage)).not.toContain('token');
  });

  it('the api client applies the header centrally, and clearing removes it', async () => {
    const seen: Array<Record<string, string>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      seen.push((init?.headers ?? {}) as Record<string, string>);
      return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    }));
    setOperatorToken(TOKEN);
    await api.health();
    clearOperatorToken();
    await api.health();
    expect(seen[0].Authorization).toBe(`Bearer ${TOKEN}`);
    expect('Authorization' in seen[1]).toBe(false);   // cleared → no header
  });

  it('401 is a typed auth error, distinct from offline', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      ({ ok: false, status: 401, clone: () => ({ json: async () => ({}) }),
         text: async () => '', json: async () => ({}) } as unknown as Response)));
    await expect(api.health()).rejects.toSatisfy((e: unknown) => isAuthError(e));
    // The backend answered — it is AVAILABLE, not offline.
    expect(deriveBackendState(false, true, true)).toBe('available');
    expect(deriveBackendState(false, true, false)).toBe('unavailable');
  });

  it('auth errors never contain the token', async () => {
    setOperatorToken(TOKEN);
    const err = new ApiAuthError(401, '/live/status');
    expect(err.message).not.toContain(TOKEN);
  });
});

describe('AuthSessionPanel', () => {
  it('enters and clears a token without rendering it', () => {
    render(<AuthSessionPanel />);
    expect(screen.getByTestId('auth-session-state').textContent).toBe('NO TOKEN');
    const input = screen.getByTestId('auth-token-input') as HTMLInputElement;
    expect(input.type).toBe('password');              // never displayed
    fireEvent.change(input, { target: { value: TOKEN } });
    fireEvent.click(screen.getByTestId('auth-token-apply'));
    expect(hasOperatorToken()).toBe(true);
    expect(screen.getByTestId('auth-session-state').textContent).toContain('TOKEN HELD');
    expect(input.value).toBe('');                     // wiped from component state
    expect(document.body.innerHTML).not.toContain(TOKEN);
    fireEvent.click(screen.getByTestId('auth-token-clear'));
    expect(hasOperatorToken()).toBe(false);
  });

  it('offers no execution controls', () => {
    render(<AuthSessionPanel />);
    const labels = Array.from(document.querySelectorAll('button')).map(
      (b) => (b.textContent ?? '').toLowerCase());
    for (const banned of ['kill', 'close', 'pause', 'resume', 'order', 'arm']) {
      expect(labels.some((l) => l.includes(banned))).toBe(false);
    }
  });
});
