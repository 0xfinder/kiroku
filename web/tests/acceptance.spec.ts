import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { mkdirSync } from 'node:fs';
import { z } from 'zod';
import { sessionSchema, ruleSchema } from '../src/lib/api';

declare global {
  interface Window {
    reconcileHints: number;
  }
}

function action(name: string): string {
  return execFileSync(
    resolve('../.venv/bin/python'),
    ['-m', 'tests.fakes.web_server', '--action', name],
    { cwd: resolve('..'), encoding: 'utf8' },
  );
}

test('phone live updates, Notes links, Rules, Review persistence and undo', async ({
  page,
  request,
}, testInfo) => {
  await page.addInitScript(() => {
    window.reconcileHints = 0;
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        this.addEventListener('reconcile', () => window.reconcileHints++);
      }
    };
  });
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.text().includes('Content Security Policy')) errors.push(msg.text());
  });
  await page.goto('/');
  await page.getByRole('link', { name: 'Owner login' }).click();
  await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
  page.on('pageerror', (error) => errors.push(error.message));
  const sessions = z
    .array(sessionSchema)
    .parse(await (await request.get('/api/sessions')).json());
  const session = sessions[0];
  if (!session) throw new Error('fixture has no session');
  await page.setViewportSize({ width: 390, height: 844 });
  const transcriptOffsets: number[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/transcript?'))
      transcriptOffsets.push(
        Number(new URL(req.url()).searchParams.get('after_ms')),
      );
  });
  await page.goto(`/session/${session.id}`);
  await expect(page.getByTestId('transcript')).toContainText(
    'Draft discussion 35',
  );
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect.poll(() => transcriptOffsets.length).toBe(2);
  expect(transcriptOffsets).toEqual([-1, 175000]);
  let navigations = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations++;
  });
  let transcriptReads = 0;
  page.on('request', (req) => {
    if (req.url().includes('/transcript?')) transcriptReads++;
  });
  const readsBeforeAppend = transcriptReads;
  action('append');
  await expect(page.getByTestId('transcript')).toContainText(
    'Live speech arrived without a reload',
  );
  expect(transcriptReads).toBe(readsBeforeAppend);
  // Hold the end of a historical scan after it has passed the older insertion point.
  let hold = true;
  const { promise: atTail, resolve: reached } = Promise.withResolvers<void>();
  const { promise: resume, resolve: release } = Promise.withResolvers<void>();
  await page.route('**/api/sessions/*/transcript?*', async (route) => {
    const url = new URL(route.request().url());
    if (
      hold &&
      url.searchParams.get('limit') === '500' &&
      url.searchParams.get('after_ms') === '180000'
    ) {
      hold = false;
      const response = await route.fetch();
      reached();
      await resume;
      await route.fulfill({ response });
    } else await route.continue();
  });
  action('backfill');
  await atTail;
  const previousHints = await page.evaluate(() => window.reconcileHints);
  action('backfill-second');
  await expect
    .poll(() => page.evaluate(() => window.reconcileHints))
    .toBeGreaterThan(previousHints);
  release();
  await expect(page.getByTestId('transcript')).toContainText(
    'Second Backfill appeared',
  );
  await page.unroute('**/api/sessions/*/transcript?*');
  await expect(page.getByTestId('transcript')).toContainText(
    'Historical Backfill appeared',
  );
  expect(navigations).toBe(0);
  expect(transcriptOffsets.filter((at) => at === -1)).toHaveLength(1);
  expect(transcriptOffsets).toContain(999);
  expect(transcriptOffsets).toContain(1999);
  expect(
    await page
      .getByTestId('transcript')
      .getByText('Live speech arrived without a reload')
      .count(),
  ).toBe(1);
  // A dropped connection must recover historical speech missed while offline.
  await page.context().setOffline(true);
  action('disconnect');
  await expect(
    page.getByRole('heading', { name: 'You’re offline' }),
  ).toBeVisible();
  action('backfill-third');
  await page.context().setOffline(false);
  await expect(page.getByTestId('transcript')).toContainText(
    'Offline Backfill appeared',
  );
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await expect
    .poll(() => transcriptOffsets.filter((at) => at === -1).length)
    .toBeGreaterThanOrEqual(2);
  // Reading older speech pauses following; the explicit control returns to live.
  await page.getByTestId('transcript').evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(
    page.getByRole('button', { name: 'Jump to live' }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Jump to live' }).click();
  await expect(page.getByRole('button', { name: 'Jump to live' })).toHaveCount(
    0,
  );
  await page.goto(`/notes?session=${session.id}`);
  await expect(
    page.getByText("Blue's range is the draft's defining advantage."),
  ).toBeVisible();
  await page.getByRole('link', { name: 'Jump to 0:00 in transcript' }).click();
  await expect(
    page.getByRole('button', { name: 'Jump to live' }),
  ).toBeVisible();
  await page.goto('/rules');
  await page.getByRole('button', { name: 'predicts winner' }).click();
  await page
    .getByLabel('Instructions', { exact: true })
    .fill('Does the streamer name a favorite?');
  // A CLI disable after the form loaded must survive a wording-only save.
  const disable = await page
    .context()
    .request.post('/api/rules/predicts_winner/disable');
  expect(disable.ok()).toBe(true);
  await page.getByRole('button', { name: 'Save Rule' }).click();
  await expect(page.getByRole('status')).toContainText('Rule saved');
  expect(
    z
      .array(ruleSchema)
      .parse(await (await request.get('/api/rules')).json())
      .find((rule) => rule.name === 'predicts_winner')?.enabled,
  ).toBe(false);
  await page
    .getByRole('button', { name: 'Test against recent windows' })
    .click();
  await expect(page.getByRole('alertdialog')).toContainText('Estimated cost');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.route('**/api/rules/predicts_winner/test', (route) =>
    route.fulfill({
      status: 202,
      json: { id: 'browser-job', status: 'running', completed: 0, total: 2 },
    }),
  );
  let polls = 0;
  await page.route('**/api/rule-tests/browser-job', (route) => {
    polls++;
    if (polls === 2)
      return route.fulfill({
        status: 503,
        json: { detail: 'Results temporarily unavailable' },
      });
    return route.fulfill({
      json:
        polls === 1
          ? { id: 'browser-job', status: 'running', completed: 1, total: 2 }
          : {
              id: 'browser-job',
              status: 'completed',
              completed: 2,
              total: 2,
              report: {
                question: 'Favorite?',
                threshold: 0.7,
                trials: [],
                scored: 2,
                passed: 1,
                failed: 0,
                pass_rate: 0.5,
                histogram: [1, 0, 0, 0, 0, 0, 0, 0, 1, 0],
                highest: [],
                lowest: [],
                report: '1 of 2',
              },
            },
    });
  });
  await page
    .getByRole('button', { name: 'Test against recent windows' })
    .click();
  await page.getByRole('button', { name: 'Confirm paid test' }).click();
  await expect(page.getByRole('status')).toContainText('1 of 2 Windows tested');
  await expect(page.getByRole('alert')).toContainText(
    'Results temporarily unavailable',
  );
  await page.getByRole('button', { name: 'Resume test results' }).click();
  await expect(
    page.getByRole('heading', { name: '1 of 2 Windows pass' }),
  ).toBeVisible();
  expect(polls).toBeGreaterThanOrEqual(2);
  const shots = resolve('../docs/web/screenshots');
  mkdirSync(shots, { recursive: true });
  for (const [label, width, height] of [
    ['phone', 390, 844],
    ['desktop', 1280, 900],
  ] as const) {
    await page.setViewportSize({ width, height });
    for (const [view, path, ready] of [
      ['live', `/session/${session.id}`, 'Transcript'],
      ['notes', `/notes?session=${session.id}`, 'Notes timeline'],
      ['rules', '/rules', 'Your Rules'],
      ['review', '/review', 'A second look'],
    ]) {
      await page.goto(path);
      await expect(
        page.getByRole('heading', { name: ready, exact: true }),
      ).toBeVisible();
      if (view === 'live')
        await expect(page.getByTestId('transcript')).toContainText(
          'Live speech arrived',
        );
      if (view === 'notes')
        await expect(
          page.getByText('Range creates room for the next fight.'),
        ).toBeVisible();
      if (view === 'rules')
        await page.getByRole('button', { name: 'predicts winner' }).click();
      if (view === 'review')
        await expect(
          page.locator('[data-testid="review-card"]:not([inert])'),
        ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      const screenshot = resolve(shots, `${view}-${label}.png`);
      await page.screenshot({ path: screenshot, fullPage: true });
      await testInfo.attach(`${view}-${label}`, {
        path: screenshot,
        contentType: 'image/png',
      });
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/review');
  await page.getByRole('button', { name: 'Yes, it applies' }).click();
  await expect(page.getByRole('status')).toContainText('Labelled yes');
  expect(JSON.parse(action('labels'))).toEqual([[true, 'review']]);
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(
    page.locator('[data-testid="review-card"]:not([inert])'),
  ).toContainText('70%');
  expect(JSON.parse(action('labels'))).toEqual([]);
  await page.getByRole('button', { name: 'No, it doesn’t' }).click();
  await expect(page.getByRole('status')).toContainText('Labelled no');
  expect(JSON.parse(action('labels'))).toEqual([[false, 'review']]);
  expect(errors).toEqual([]);
});

test('public read-only PWA, CSP, network-only data and offline shell', async ({
  page,
  context,
  request,
}, testInfo) => {
  mkdirSync(resolve('../docs/web/screenshots'), { recursive: true });
  const violations: string[] = [];
  page.on('console', (msg) => {
    if (msg.text().includes('Content Security Policy'))
      violations.push(msg.text());
  });
  page.on('pageerror', (error) => violations.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/rules');
  await expect(page.getByRole('link', { name: 'Owner login' })).toBeVisible();
  await expect(
    page.getByRole('button', { name: /New Rule|Save Rule|Test against/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole('link', { name: 'Review', exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole('heading', { name: 'predicts winner', exact: true }),
  ).toBeVisible();
  expect((await request.get('/api/review')).status()).toBe(403);
  expect(
    (
      await request.post('/api/rules', {
        data: { name: 'public', instructions: 'Question?' },
      })
    ).status(),
  ).toBe(403);
  expect((await request.get('/api/rule-tests/unknown')).status()).toBe(403);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: resolve('../docs/web/screenshots/public-rules-phone.png'),
    fullPage: true,
  });
  await page.goto('/review');
  await expect(
    page.getByRole('heading', { name: 'Owner login required' }),
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: /Yes|No|Undo|Check for more/ }),
  ).toHaveCount(0);
  const manifest = await request.get('/manifest.webmanifest');
  expect(manifest.ok()).toBe(true);
  const parsed = z
    .object({
      name: z.string(),
      display: z.literal('standalone'),
      icons: z.array(
        z.object({
          src: z.string(),
          sizes: z.string(),
          purpose: z.string().optional(),
        }),
      ),
    })
    .parse(await manifest.json());
  expect(parsed.icons.map((icon) => icon.sizes)).toContain('192x192');
  expect(parsed.icons.map((icon) => icon.sizes)).toContain('512x512');
  expect(parsed.icons.some((icon) => icon.purpose === 'maskable')).toBe(true);
  for (const icon of [
    ...parsed.icons.map((icon) => icon.src),
    '/icons/apple-touch-icon.png',
  ])
    expect((await request.get(icon)).ok()).toBe(true);
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await expect
    .poll(() =>
      page.evaluate(() => navigator.serviceWorker.controller !== null),
    )
    .toBe(true);
  // Reinstall with an obsolete cache present to exercise activation cleanup.
  await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    await registration.unregister();
    await caches.open('kiroku-shell-obsolete');
  });
  await page.reload();
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await expect
    .poll(() =>
      page.evaluate(async () =>
        (await caches.keys()).includes('kiroku-shell-obsolete'),
      ),
    )
    .toBe(false);
  await page.goto('/');
  await expect(
    page.getByRole('heading', { name: 'Live & recent sessions' }),
  ).toBeVisible();
  await page.evaluate(async () => {
    await fetch('/api/sessions');
    await fetch('/api/me');
    await fetch('/auth/login', { redirect: 'manual' });
  });
  const cachedPaths = await page.evaluate(async () => {
    const paths: string[] = [];
    for (const name of await caches.keys())
      for (const key of await (await caches.open(name)).keys())
        paths.push(new URL(key.url).pathname);
    return paths;
  });
  expect(cachedPaths).toContain('/');
  expect(cachedPaths.some((path) => /^\/(api|auth)(\/|$)/.test(path))).toBe(
    false,
  );
  await context.setOffline(true);
  await page.reload();
  await expect(
    page.getByRole('heading', { name: 'You’re offline' }),
  ).toBeVisible();
  await expect(page.getByText('caedrel', { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(async () => {
      try {
        await fetch('/api/sessions');
        return 'cached';
      } catch {
        return 'network failed';
      }
    }),
  ).toBe('network failed');
  expect(
    await page.evaluate(async () => {
      try {
        await fetch('/auth/login');
        return 'cached';
      } catch {
        return 'network failed';
      }
    }),
  ).toBe('network failed');
  await page.goto('/notes');
  await expect(
    page.getByRole('heading', { name: 'You’re offline' }),
  ).toBeVisible();
  const offlineShot = resolve('../docs/web/screenshots/offline-phone.png');
  await page.screenshot({ path: offlineShot, fullPage: true });
  await testInfo.attach('offline-phone', {
    path: offlineShot,
    contentType: 'image/png',
  });
  await context.setOffline(false);
  await expect(
    page.getByRole('heading', { name: 'Notes timeline' }),
  ).toBeVisible();
  await page.getByRole('link', { name: 'Owner login' }).click();
  await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
  await page.getByRole('button', { name: 'Log out' }).click();
  await expect(page.getByRole('link', { name: 'Owner login' })).toBeVisible();
  expect((await context.request.get('/api/me')).ok()).toBe(true);
  expect(await (await context.request.get('/api/me')).json()).toEqual({
    owner: false,
  });
  expect(violations).toEqual([]);
});

test('owner identity retries /me and /auth errors without a public downgrade', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('link', { name: 'Owner login' }).click();
  await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
  for (const path of ['me', 'auth']) {
    let attempts = 0;
    await page.route(`**/api/${path}`, (route) => {
      attempts++;
      return attempts <= 3
        ? route.fulfill({
            status: attempts === 1 ? 429 : attempts === 2 ? 503 : 500,
            json: { detail: 'busy' },
          })
        : route.continue();
    });
    await page.reload();
    await expect(
      page.getByText('Checking owner login…', { exact: true }),
    ).toBeVisible();
    await expect(page.getByText('Read-only', { exact: true })).toHaveCount(0);
    await expect(
      page.getByText('Unable to check owner login; retrying.'),
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible({
      timeout: 15000,
    });
    expect(attempts).toBe(4);
    await page.unroute(`**/api/${path}`);
  }
});

test('429 and 503 permanently closed streams retry and reconnect', async ({
  page,
  request,
}) => {
  const [session] = z
    .array(sessionSchema)
    .parse(await (await request.get('/api/sessions')).json());
  if (!session) throw new Error('missing fixture session');
  let attempts = 0;
  await page.route('**/transcript/stream?*', (route) => {
    attempts++;
    return attempts <= 2
      ? route.fulfill({
          status: attempts === 1 ? 429 : 503,
          json: { detail: 'too many streams' },
        })
      : route.continue();
  });
  await page.goto(`/session/${session.id}`);
  await expect(
    page.getByText('Stream unavailable or too many open streams; retrying'),
  ).toBeVisible();
  await expect(page.getByText('Connected', { exact: true })).toBeVisible({
    timeout: 10000,
  });
  expect(attempts).toBe(3);
});

test('repeated failures go offline and recover automatically when visible', async ({
  page,
}) => {
  await page.goto('/notes');
  await expect(page.getByRole('link', { name: 'Owner login' })).toBeVisible();
  await page.route('**/api/**', (route) => route.abort('connectionfailed'));
  await page.evaluate(() =>
    document.dispatchEvent(new Event('visibilitychange')),
  );
  await expect(
    page.getByRole('heading', { name: 'You’re offline' }),
  ).toBeVisible({ timeout: 10000 });
  expect(await page.evaluate(() => navigator.onLine)).toBe(true);
  await page.unrouteAll({ behavior: 'wait' });
  // No manual Retry: the scheduled probe recovers without any event.
  await expect(
    page.getByRole('heading', { name: 'Notes timeline' }),
  ).toBeVisible({ timeout: 15000 });
  await page.route('**/api/**', (route) => route.abort('connectionfailed'));
  await page.evaluate(() =>
    document.dispatchEvent(new Event('visibilitychange')),
  );
  await expect(
    page.getByRole('heading', { name: 'You’re offline' }),
  ).toBeVisible({ timeout: 10000 });
  await page.unrouteAll({ behavior: 'wait' });
  await page.evaluate(() =>
    document.dispatchEvent(new Event('visibilitychange')),
  );
  await expect(
    page.getByRole('heading', { name: 'Notes timeline' }),
  ).toBeVisible();
});
