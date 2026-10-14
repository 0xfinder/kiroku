import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { z } from 'zod';
import {
  sessionSchema,
  passSchema,
  utteranceSchema,
  reviewSchema,
} from '../src/lib/api';

// Use the acceptance harness's disposable database and real transcript commit/SSE path.
function appendSpeech(text: string) {
  execFileSync(
    resolve('../.venv/bin/python'),
    [
      '-c',
      `
import asyncio, json, sys
from tests.fakes.web_server import STATE, url_for, TRANSCRIBER
from kiroku.db import open_pool
from kiroku import transcript
from kiroku.transcriber import Result, Utterance
async def main():
    state = json.loads(STATE.read_text())
    async with open_pool(url_for(state['name'])) as pool:
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT max(audio_end_ms) FROM utterance WHERE session_id=%s', [state['session']])).fetchone()
        start = row[0] + 1000
        await transcript.commit(pool, state['session'], TRANSCRIBER, Result(start, start+5000, (Utterance(start, start+4000, sys.argv[1]),), None))
asyncio.run(main())
`,
      text,
    ],
    { cwd: resolve('..') },
  );
}

for (const [label, viewport, reducedMotion] of [
  ['desktop', { width: 1440, height: 900 }, 'no-preference'],
  ['phone', { width: 390, height: 844 }, 'no-preference'],
  ['reduced', { width: 390, height: 844 }, 'reduce'],
] as const) {
  test(`live motion, reconnect, pin and Review undo — ${label}`, async ({
    browser,
    request,
  }) => {
    const artifacts = '/tmp/web-motion';
    mkdirSync(artifacts, { recursive: true });
    const context = await browser.newContext({
      baseURL: 'https://127.0.0.1:8748',
      ignoreHTTPSErrors: true,
      viewport,
      reducedMotion,
      recordVideo:
        reducedMotion === 'reduce'
          ? undefined
          : { dir: artifacts, size: viewport },
    });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => {
      if (message.text().includes('Content Security Policy'))
        errors.push(message.text());
    });
    const [session] = z
      .array(sessionSchema)
      .parse(await (await request.get('/api/sessions')).json());
    if (!session) throw new Error('missing fixture session');
    try {
      await page.goto('/');
      await page.getByRole('link', { name: 'Owner login' }).click();
      await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
      // Restore the fixture verdict left by acceptance.spec.ts so both cards are available.
      const fixture = z
        .object({ decision: z.number() })
        .parse(JSON.parse(readFileSync(resolve('../.web-e2e.json'), 'utf8')));
      const restored = await context.request.delete(
        `/api/review/${fixture.decision}`,
        { headers: { Origin: 'https://127.0.0.1:8748' } },
      );
      expect(restored.ok()).toBe(true);
      await page.goto(`/session/${session.id}`);
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      expect(
        await page
          .locator('.utterance')
          .evaluateAll(
            (nodes) => nodes.flatMap((node) => node.getAnimations()).length,
          ),
      ).toBe(0);
      // Observe WAAPI transitions in the browser; production needs no test-only hooks.
      await page.evaluate(() => {
        const seen = new WeakSet<Animation>();
        const observe = () => {
          for (const node of document.querySelectorAll('.utterance')) {
            for (const animation of node.getAnimations()) {
              if (seen.has(animation)) continue;
              seen.add(animation);
              node.setAttribute(
                'data-enter-count',
                String(Number(node.getAttribute('data-enter-count') ?? 0) + 1),
              );
            }
          }
          requestAnimationFrame(observe);
        };
        requestAnimationFrame(observe);
      });
      const status = await page
        .locator('.connection-status[role="status"]')
        .elementHandle();
      if (!status) throw new Error('missing persistent connection status');
      const text = `A new ${label} Utterance: Blue takes the next objective.`;
      appendSpeech(text);
      const arrival = page.locator('.utterance').filter({ hasText: text });
      await expect(arrival).toBeVisible();
      if (reducedMotion === 'reduce') {
        await expect(arrival).not.toHaveAttribute('data-enter-count');
        expect(
          await arrival.evaluate((element) => element.getAnimations().length),
        ).toBe(0);
      } else await expect(arrival).toHaveAttribute('data-enter-count', '1');
      await expect
        .poll(() =>
          arrival.evaluate((element) => element.getAnimations().length),
        )
        .toBe(0);
      await expect(
        page.getByRole('button', { name: 'Jump to live' }),
      ).toHaveCount(0);
      await expect
        .poll(() =>
          page
            .getByTestId('transcript')
            .evaluate(
              (element) =>
                element.scrollHeight - element.clientHeight - element.scrollTop,
            ),
        )
        .toBeLessThan(2);
      await page.screenshot({
        path: `${artifacts}/session-${label}.png`,
        fullPage: true,
      });
      // Keep references to the keyed DOM nodes: reconnect must reuse them without a second enter.
      await page.evaluate(() => {
        for (const node of document.querySelectorAll('.utterance'))
          node.setAttribute('data-before-reconnect', 'true');
      });
      let scans = 0;
      page.on('request', (req) => {
        if (req.url().includes('/transcript?after_ms=-1')) scans++;
      });
      execFileSync(
        resolve('../.venv/bin/python'),
        ['-m', 'tests.fakes.web_server', '--action', 'disconnect'],
        { cwd: resolve('..') },
      );
      await expect.poll(() => scans).toBeGreaterThan(0);
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      await expect(arrival).toHaveAttribute('data-before-reconnect', 'true');
      expect(
        await status.evaluate(
          (node) =>
            node ===
            document.querySelector('.connection-status[role="status"]'),
        ),
      ).toBe(true);
      if (reducedMotion !== 'reduce')
        await expect(arrival).toHaveAttribute('data-enter-count', '1');
      await expect
        .poll(() =>
          arrival.evaluate((element) => element.getAnimations().length),
        )
        .toBe(0);
      await page.getByTestId('transcript').hover();
      for (let i = 0; i < 3; i++) await page.mouse.wheel(0, -200);
      await expect
        .poll(() =>
          page
            .getByTestId('transcript')
            .evaluate(
              (element) =>
                element.scrollHeight - element.clientHeight - element.scrollTop,
            ),
        )
        .toBeGreaterThan(80);
      await expect(
        page.getByRole('button', { name: 'Jump to live' }),
      ).toBeVisible();
      // Finish scrolling to the start, then prove a later arrival preserves the reader's position.
      await page.getByTestId('transcript').evaluate((element) => {
        element.scrollTop = 0;
      });
      await expect(
        page.getByRole('button', { name: 'Jump to live' }),
      ).toBeVisible();
      appendSpeech(
        `While ${label} reads earlier speech, this line stays at the bottom.`,
      );
      await expect(page.getByTestId('transcript')).toContainText(
        `While ${label} reads earlier speech`,
      );
      expect(
        await page
          .getByTestId('transcript')
          .evaluate((element) => element.scrollTop),
      ).toBe(0);
      // Exercise the target/timeout fallback even when the browser never delivers scrollend.
      await page
        .getByTestId('transcript')
        .evaluate((node) =>
          node.addEventListener(
            'scrollend',
            (event) => event.stopImmediatePropagation(),
            { capture: true },
          ),
        );
      await page.getByRole('button', { name: 'Jump to live' }).click();
      await expect
        .poll(() =>
          page
            .getByTestId('transcript')
            .evaluate(
              (element) =>
                element.scrollHeight - element.clientHeight - element.scrollTop,
            ),
        )
        .toBeLessThan(2);
      // Client-side route navigation exercises View Transitions and closes the session SSE.
      let streamRequests = 0;
      page.on('request', (req) => {
        if (req.url().includes('/transcript/stream?')) streamRequests++;
      });
      await page.getByRole('link', { name: 'Review', exact: true }).click();
      await expect(
        page.locator('[data-testid="review-card"]:not([inert])'),
      ).toBeVisible();
      const [first, next] = z
        .array(reviewSchema)
        .parse(
          await (await context.request.get('/api/review?limit=100')).json(),
        );
      if (!first || !next)
        throw new Error('keyboard Review needs two fixture cards');
      const card = page.locator(`[data-review-id="${first.id}"]`);
      const cardText = await card.innerText();
      const yes = card.getByRole('button', { name: 'Yes, it applies' });
      await yes.focus();
      await yes.press('Enter');
      await expect(page.getByRole('status')).toContainText('Labelled yes');
      await expect(
        page
          .locator(`[data-review-id="${next.id}"]`)
          .getByRole('button', { name: 'Yes, it applies' }),
      ).toBeFocused();
      await page.getByRole('button', { name: 'Undo', exact: true }).click();
      await expect(card).toHaveText(cardText, { useInnerText: true });
      await expect(yes).toBeFocused();
      await page.screenshot({
        path: `${artifacts}/review-${label}.png`,
        fullPage: true,
      });
      await page.getByRole('link', { name: 'Notes', exact: true }).click();
      await expect(
        page.getByRole('heading', { name: 'Notes timeline' }),
      ).toBeVisible();
      await expect(
        page.getByText("Blue's range is the draft's defining advantage."),
      ).toBeVisible();
      await page.screenshot({
        path: `${artifacts}/route-notes-${label}.png`,
        fullPage: true,
      });
      await page
        .getByRole('link', { name: 'Jump to 0:00 in transcript' })
        .click();
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      expect(streamRequests).toBe(1);
      if (reducedMotion === 'reduce')
        expect(await page.evaluate(() => document.getAnimations().length)).toBe(
          0,
        );
      expect(errors).toEqual([]);
    } finally {
      const video = page.video();
      await context.close();
      if (video) await video.saveAs(`${artifacts}/r2-${label}.webm`);
    }
  });
}

test('cursor and snapshot failures settle loading; Retry recovers an empty session', async ({
  page,
  request,
}) => {
  const [session] = z
    .array(sessionSchema)
    .parse(await (await request.get('/api/sessions')).json());
  if (!session) throw new Error('missing fixture session');
  let failCursor = true;
  let failSnapshot = true;
  await page.route(`**/api/sessions/${session.id}/transcript/cursor`, (route) =>
    route.fulfill({
      status: failCursor ? 500 : 200,
      json: failCursor ? { detail: 'Cursor unavailable' } : { id: 0 },
    }),
  );
  await page.route(`**/api/sessions/${session.id}/transcript?**`, (route) =>
    route.fulfill({
      status: failSnapshot ? 500 : 200,
      json: failSnapshot ? { detail: 'Snapshot unavailable' } : [],
    }),
  );
  await page.route(
    `**/api/sessions/${session.id}/transcript/stream?**`,
    (route) =>
      route.fulfill({
        contentType: 'text/event-stream',
        body: ': empty session\n\n',
      }),
  );
  await page.goto(`/session/${session.id}`);
  await expect(page.getByRole('alert')).toContainText('Cursor unavailable');
  await expect(
    page.getByRole('status', { name: 'Loading transcript…' }),
  ).toHaveCount(0);
  await expect(page.getByText('Waiting for speech…')).toBeVisible();
  failCursor = false;
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Snapshot unavailable');
  await expect(
    page.getByRole('status', { name: 'Loading transcript…' }),
  ).toHaveCount(0);
  failSnapshot = false;
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByText('Waiting for speech…')).toBeVisible();
  await expect(
    page.getByRole('status', { name: 'Loading transcript…' }),
  ).toHaveCount(0);
});

test('a live trailing event stays pinned and keeps its node before new speech', async ({
  page,
  request,
}) => {
  const [session] = z
    .array(sessionSchema)
    .parse(await (await request.get('/api/sessions')).json());
  if (!session) throw new Error('missing fixture session');
  const speech = z
    .array(utteranceSchema)
    .parse(
      await (
        await request.get(
          `/api/sessions/${session.id}/transcript?after_ms=-1&limit=500`,
        )
      ).json(),
    );
  const passes = z
    .array(passSchema)
    .parse(
      await (await request.get(`/api/sessions/${session.id}/passes`)).json(),
    );
  let published = false;
  await page.route(`**/api/sessions/${session.id}/passes`, (route) =>
    route.fulfill({
      json: published
        ? [
            ...passes,
            {
              id: 987654,
              rule: 'live_event_identity',
              question: 'Fixture event',
              probability: 0.9,
              threshold: 0.6,
              start_ms: (speech.at(-1)?.audio_start_ms ?? 0) + 500,
            },
          ]
        : passes,
    }),
  );
  await page.clock.install();
  await page.goto(`/session/${session.id}`);
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  published = true;
  await page.clock.fastForward(30000);
  const event = page
    .locator('.inline-event')
    .filter({ hasText: 'live_event_identity' });
  await expect(event).toHaveCount(1);
  await expect(event).toBeVisible();
  await expect
    .poll(() =>
      page
        .getByTestId('transcript')
        .evaluate(
          (element) =>
            element.scrollHeight - element.clientHeight - element.scrollTop,
        ),
    )
    .toBeLessThan(2);
  const original = await event.elementHandle();
  if (!original) throw new Error('missing live inline event');
  appendSpeech('Speech after the trailing inline event.');
  const arrival = page
    .locator('.utterance')
    .filter({ hasText: 'Speech after the trailing inline event.' });
  await expect(arrival).toBeAttached();
  await page.clock.runFor(650);
  await expect(event).toHaveCount(1);
  expect(
    await original.evaluate(
      (node) =>
        node ===
        [...document.querySelectorAll('.inline-event')].find((element) =>
          element.textContent?.includes('live_event_identity'),
        ),
    ),
  ).toBe(true);
  expect(
    await original.evaluate((node) =>
      node.nextElementSibling?.textContent?.includes(
        'Speech after the trailing inline event.',
      ),
    ),
  ).toBe(true);
});
