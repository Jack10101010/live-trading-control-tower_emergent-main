/**
 * UI-0 — a backend outage must produce an explicit, retryable disconnected state,
 * never a blank page and never a silent switch to fixtures.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AppErrorBoundary, classifyFailure } from '@/components/AppErrorBoundary';

function Boom({ error }: { error: unknown }): JSX.Element {
  throw error;
}

function renderBoom(error: unknown, onRetry?: () => void) {
  // The boundary logs to console.error by design; keep test output clean.
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
  const utils = render(
    <AppErrorBoundary onRetry={onRetry}>
      <Boom error={error} />
    </AppErrorBoundary>
  );
  spy.mockRestore();
  return utils;
}

describe('failure classification', () => {
  it('treats a transport failure as backend-unavailable', () => {
    expect(classifyFailure(new TypeError('Failed to fetch'))).toBe('backend-unavailable');
    expect(classifyFailure(new TypeError('Load failed'))).toBe('backend-unavailable');
  });

  it('treats a 5xx API error as a failed request', () => {
    expect(classifyFailure(new Error('API 500 /fleet: boom'))).toBe('request-failed');
  });

  it('distinguishes a missing fixture world', () => {
    expect(classifyFailure(new Error('API 404 /world: not found'))).toBe('fixture-unavailable');
  });

  it('falls back to unknown for anything else', () => {
    expect(classifyFailure(new Error('something odd'))).toBe('unknown');
  });
});

describe('AppErrorBoundary', () => {
  it('renders a visible error state instead of a blank page', () => {
    renderBoom(new TypeError('Failed to fetch'));
    const state = screen.getByTestId('app-error-state');
    expect(state).toBeTruthy();
    expect(state.getAttribute('data-failure-kind')).toBe('backend-unavailable');
    expect(screen.getByText(/backend unreachable/i)).toBeTruthy();
  });

  it('offers a retry action that invokes the reset callback', () => {
    const onRetry = vi.fn();
    renderBoom(new TypeError('Failed to fetch'), onRetry);
    const btn = screen.getByTestId('app-error-retry');
    btn.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('never substitutes demonstration data and says so', () => {
    renderBoom(new Error('API 500 /fleet: boom'));
    expect(screen.getByText(/does not substitute demonstration data/i)).toBeTruthy();
  });

  it('does not leak a stack trace to the operator', () => {
    renderBoom(new Error('API 500 /fleet: Traceback (most recent call last): secret'));
    const state = screen.getByTestId('app-error-state');
    expect(state.textContent).not.toContain('Traceback');
    expect(state.textContent).not.toContain('secret');
  });

  it('preserves shell context so the operator knows the UI itself is running', () => {
    renderBoom(new TypeError('Failed to fetch'));
    // The shell wordmark stays visible so it is obvious the UI itself is running.
    expect(screen.getByText('Control Tower')).toBeTruthy();
    expect(screen.getByText('Disconnected')).toBeTruthy();
  });
});
