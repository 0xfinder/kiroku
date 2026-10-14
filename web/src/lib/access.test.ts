import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { get } from 'svelte/store';
import { access, identityError, loginAvailable, monitorAccess } from './access';
import { connectionSucceeded, serverAvailable } from './connectivity';

let monitor: ReturnType<typeof monitorAccess> | undefined;
let windowEvents: EventTarget;
let documentEvents: EventTarget;
beforeEach(() => {
  vi.useFakeTimers();
  windowEvents = new EventTarget();
  documentEvents = new EventTarget();
  vi.stubGlobal('window', windowEvents);
  vi.stubGlobal(
    'document',
    Object.assign(documentEvents, { visibilityState: 'visible' }),
  );
  vi.stubGlobal('navigator', { onLine: true });
  connectionSucceeded();
});
afterEach(() => {
  monitor?.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const success = (url: string) =>
  Promise.resolve(
    Response.json(
      url.endsWith('/me')
        ? { owner: true, email: 'owner@example.test' }
        : { login: true },
    ),
  );

test.each([429, 503, 500])(
  'identity stays unknown and recovers after /me returns %s',
  async (status) => {
    let attempts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.endsWith('/me') && ++attempts <= 3)
          return Promise.resolve(Response.json({ detail: 'busy' }, { status }));
        return success(url);
      }),
    );
    monitor = monitorAccess();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(access)).toEqual({ owner: null });
    expect(get(loginAvailable)).toBe(false);
    expect(attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(6000);
    expect(attempts).toBe(3);
    expect(get(identityError)).toContain('retrying');
    await vi.advanceTimersByTimeAsync(8000);
    expect(get(access)).toEqual({ owner: true, email: 'owner@example.test' });
    expect(get(identityError)).toBe('');
    expect(get(loginAvailable)).toBe(true);
  },
);

test('failed /auth also retries instead of permanently downgrading the owner', async () => {
  let attempts = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      url.endsWith('/auth') && ++attempts === 1
        ? Promise.resolve(Response.json({ detail: 'busy' }, { status: 503 }))
        : success(url),
    ),
  );
  monitor = monitorAccess();
  await vi.advanceTimersByTimeAsync(0);
  expect(get(access).owner).toBe(null);
  await vi.advanceTimersByTimeAsync(2000);
  expect(get(access).owner).toBe(true);
});

test.each(['timer', 'visibility'])(
  'offline automatically probes and recovers via %s',
  async (trigger) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Network failed')),
    );
    monitor = monitorAccess();
    await vi.advanceTimersByTimeAsync(6000);
    expect(get(serverAvailable)).toBe(false);
    vi.stubGlobal('fetch', vi.fn(success));
    if (trigger === 'visibility') {
      documentEvents.dispatchEvent(new Event('visibilitychange'));
      await vi.advanceTimersByTimeAsync(0);
    } else await vi.advanceTimersByTimeAsync(8000);
    expect(get(serverAvailable)).toBe(true);
    expect(get(access).owner).toBe(true);
    monitor.stop();
    expect(vi.getTimerCount()).toBe(0);
  },
);

test('late identity responses cannot restore owner controls after going offline', async () => {
  let resolveMe: ((response: Response) => void) | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      url.endsWith('/me')
        ? new Promise<Response>((resolve) => {
            resolveMe = resolve;
          })
        : success(url),
    ),
  );
  monitor = monitorAccess();
  vi.stubGlobal('navigator', { onLine: false });
  windowEvents.dispatchEvent(new Event('offline'));
  resolveMe?.(Response.json({ owner: true, email: 'owner@example.test' }));
  await vi.advanceTimersByTimeAsync(0);
  expect(get(access).owner).toBe(null);
  expect(get(serverAvailable)).toBe(false);
});
