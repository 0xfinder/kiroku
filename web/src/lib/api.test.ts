import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { get } from 'svelte/store';
import { request } from './api';
import { serverAvailable } from './connectivity';

beforeEach(() => serverAvailable.set(true));
afterEach(() => vi.unstubAllGlobals());

test('network and response-body failures hide data until explicit recovery', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockRejectedValue(new TypeError('Network failed')),
  );
  await expect(request('/sessions')).rejects.toThrow('Network failed');
  expect(get(serverAvailable)).toBe(false);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json([])));
  expect(await request('/sessions')).toEqual([]);
  expect(get(serverAvailable)).toBe(false);
  serverAvailable.set(true);
  const body = new ReadableStream({
    start(controller) {
      controller.error(new TypeError('Connection closed'));
    },
  });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
  await expect(request('/sessions')).rejects.toThrow('Connection closed');
  expect(get(serverAvailable)).toBe(false);
});

test('invalid JSON and application errors stay distinct from connection failures', async () => {
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
      .mockResolvedValue(
        Response.json({ detail: 'API capacity reached' }, { status: 503 }),
      ),
  );
  await expect(request('/sessions')).rejects.toThrow('API capacity reached');
  expect(get(serverAvailable)).toBe(true);
});

test('an unavailable gateway switches to offline', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('Bad gateway', { status: 502 })),
  );
  await expect(request('/sessions')).rejects.toThrow('Request failed (502)');
  expect(get(serverAvailable)).toBe(false);
});
