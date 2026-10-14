import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { z } from 'zod';
import { sessionSchema } from '../src/lib/api';

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
      await page.goto(`/session/${session.id}`);
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      await expect(page.locator('.utterance[data-entering]')).toHaveCount(0);
      // Record the application's enter state synchronously, without relying on polling inside 300 ms.
      await page.evaluate(() => {
        document.addEventListener(
          'introstart',
          (event) => {
            const node = event.target;
            queueMicrotask(() => {
              if (
                node instanceof HTMLElement &&
                node.matches('.utterance[data-entering]')
              ) {
                node.dataset.enterCount = String(
                  Number(node.dataset.enterCount ?? 0) + 1,
                );
              }
            });
          },
          { capture: true },
        );
      });
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
      await expect(page.locator('.utterance[data-entering]')).toHaveCount(0);
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
      if (reducedMotion !== 'reduce')
        await expect(arrival).toHaveAttribute('data-enter-count', '1');
      await expect(page.locator('.utterance[data-entering]')).toHaveCount(0);
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
      const cardText = await page
        .locator('[data-testid="review-card"]:not([inert])')
        .innerText();
      await page.getByRole('button', { name: 'Yes, it applies' }).click();
      await expect(page.getByRole('status')).toContainText('Labelled yes');
      await page.getByRole('button', { name: 'Undo', exact: true }).click();
      await expect(
        page.locator('[data-testid="review-card"]:not([inert])'),
      ).toHaveText(cardText, { useInnerText: true });
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
      if (video) await video.saveAs(`${artifacts}/motion-${label}.webm`);
    }
  });
}
