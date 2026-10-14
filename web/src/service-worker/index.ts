import { immutable, assets as staticAssets } from '$app/manifest';
import { version } from '$app/env';
import { self as worker } from '$app/service-worker';
const cacheName = `kiroku-shell-${version}`;
const assets = new Set(
  [...immutable, ...staticAssets].map(
    (asset) => new URL(asset.path, worker.registration.scope).pathname,
  ),
);
assets.add('/');
worker.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(cacheName);
      await cache.addAll([...assets]);
      await worker.skipWaiting();
    })(),
  );
});
worker.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith('kiroku-shell-') && name !== cacheName)
          await caches.delete(name);
      }
      await worker.clients.claim();
    })(),
  );
});
worker.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (
    url.origin !== worker.location.origin ||
    event.request.method !== 'GET' ||
    /^\/(api|auth)(\/|$)/.test(url.pathname) ||
    event.request.headers.get('accept')?.includes('text/event-stream')
  )
    return;
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if ([502, 503, 504].includes(response.status))
            throw new Error('Origin unavailable');
          return response;
        })
        .catch(async () => {
          const shell = await caches.match('/');
          return shell ?? Response.error();
        }),
    );
  } else if (assets.has(url.pathname)) {
    event.respondWith(
      (async () =>
        (await caches.match(event.request)) ?? fetch(event.request))(),
    );
  }
});
