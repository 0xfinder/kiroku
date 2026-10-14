import { afterEach, describe, expect, it, vi } from 'vitest';
import { motionDuration, scrollBehavior, transcriptArrivals } from './motion';

afterEach(() => vi.unstubAllGlobals());
describe('motion preference', () => {
  it('uses zero duration and instant scrolling under reduced motion', () => {
    vi.stubGlobal('window', { matchMedia: () => ({ matches: true }) });
    expect(motionDuration()).toBe(0);
    expect(scrollBehavior(true)).toBe('instant');
  });
  it('uses the requested duration and smooth scrolling otherwise', () => {
    vi.stubGlobal('window', { matchMedia: () => ({ matches: false }) });
    expect(motionDuration(220)).toBe(220);
    expect(scrollBehavior(true)).toBe('smooth');
    expect(scrollBehavior(false)).toBe('instant');
  });
  it('is safe before the browser mounts', () => {
    expect(motionDuration()).toBe(300);
  });
});

it('animates only unseen forward live arrivals, including a shared audio offset', () => {
  const tracker = transcriptArrivals();
  const row = (id: number, audio_start_ms: number) => ({ id, audio_start_ms });
  expect(tracker.accept([row(1, 100), row(2, 200)], 'snapshot').size).toBe(0);
  expect([...tracker.accept([row(3, 300), row(4, 300)], 'live')]).toEqual([
    3, 4,
  ]);
  expect(tracker.accept([row(3, 300)], 'live').size).toBe(0);
  expect(tracker.accept([row(5, 50)], 'live').size).toBe(0);
  expect(tracker.accept([row(1, 100), row(6, 400)], 'reconcile').size).toBe(0);
  expect(tracker.accept([row(6, 400)], 'live').size).toBe(0);
  expect([...tracker.accept([row(7, 500)], 'live')]).toEqual([7]);
});
