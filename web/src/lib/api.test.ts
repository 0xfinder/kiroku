import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { get } from 'svelte/store';
import { request } from './api';
import { connectionSucceeded, serverAvailable } from './connectivity';

beforeEach(() => {
  vi.stubGlobal('navigator', { onLine: true });
  connectionSucceeded();
});
afterEach(() => vi.unstubAllGlobals());

test('one iOS background network failure keeps data visible; repeated failures hide it', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockRejectedValue(new TypeError('Network failed')),
  );
  await expect(request('/sessions')).rejects.toThrow('Network failed');
  expect(get(serverAvailable)).toBe(true);
  await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(true);
  await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(false);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json([])));
  expect(await request('/sessions')).toEqual([]);
  expect(get(serverAvailable)).toBe(true);
});

test('a success resets consecutive failures; response-body failures count too', async () => {
  const failed = () =>
    new Response(
      new ReadableStream({
        start(controller) {
          controller.error(new TypeError('Connection closed'));
        },
      }),
    );
  vi.stubGlobal('fetch', vi.fn().mockImplementation(failed));
  await expect(request('/sessions')).rejects.toThrow('Connection closed');
  await expect(request('/sessions')).rejects.toThrow();
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json([])));
  await request('/sessions');
  vi.stubGlobal('fetch', vi.fn().mockImplementation(failed));
  await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(true);
});

test('navigator offline hides data on the first failure', async () => {
  vi.stubGlobal('navigator', { onLine: false });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Offline')));
  await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(false);
});

test('invalid JSON and admission errors do not select the offline view', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('invalid-json')),
  );
  await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(true);
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockImplementation(async () =>
        Response.json({ detail: 'API capacity reached' }, { status: 503 }),
      ),
  );
  for (let i = 0; i < 3; i++)
    await expect(request('/sessions')).rejects.toThrow('API capacity reached');
  expect(get(serverAvailable)).toBe(true);
});

test('gateway failures require repetition and recover on the first success', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockImplementation(
        async () => new Response('Bad gateway', { status: 502 }),
      ),
  );
  await expect(request('/sessions')).rejects.toThrow('Request failed (502)');
  expect(get(serverAvailable)).toBe(true);
  for (let i = 0; i < 2; i++)
    await expect(request('/sessions')).rejects.toThrow();
  expect(get(serverAvailable)).toBe(false);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json([])));
  await request('/sessions');
  expect(get(serverAvailable)).toBe(true);
});
