import { describe, expect, it } from 'vitest';
import {
  atBottom,
  clock,
  mergeTranscript,
  removeCard,
  restoreCard,
  noteItems,
  ruleEditBody,
  transcriptCursor,
} from './helpers';
import type { Review, Utterance } from './api';

const card: Review = {
  id: 7,
  rule: 'winner',
  question: 'Winner?',
  probability: 0.5,
  threshold: 0.7,
  passed: false,
  session_id: 1,
  channel: 'caedrel',
  start_ms: 0,
  new: 'Blue wins',
  earlier: '',
};
const speech = (id: number, at: number): Utterance => ({
  id,
  audio_start_ms: at,
  audio_end_ms: at + 1000,
  text: 'speech',
  spoken_at: '2026-10-13T12:00:00Z',
  transcriber: 'fixture',
});
describe('timeline behavior', () => {
  it('formats session time past an hour and clamps negative offsets', () => {
    expect(clock(3_661_500)).toBe('61:01');
    expect(clock(-1)).toBe('0:00');
  });
  it('follows live only within reach of the bottom', () => {
    expect(atBottom(420, 500, 1000)).toBe(true);
    expect(atBottom(100, 500, 1000)).toBe(false);
  });
  it('merges reconnects and late Backfill without losing comparison transcribers', () => {
    expect(
      mergeTranscript(
        [speech(2, 2000)],
        [speech(1, 0), speech(2, 2000), speech(3, 2000)],
      ).map((r) => r.id),
    ).toEqual([1, 2, 3]);
  });
  it('restores an optimistic vote exactly once after failure or undo', () => {
    const other = { ...card, id: 8 };
    const queue = removeCard([card, other], 7);
    expect(queue).toEqual([other]);
    expect(restoreCard(restoreCard(queue, card), card)).toEqual([card, other]);
  });
  it('reads timestamped Pack fields without treating metadata as speech', () => {
    expect(
      noteItems({
        id: 1,
        start_ms: 0,
        end_ms: 180000,
        pack: null,
        mode: null,
        status: 'ok',
        schema_name: 'general',
        schema_version: 2,
        body: {
          summary: 'Summary',
          takes: [{ at_ms: 1000, subject: 'Draft', take: 'Blue has range' }],
          teams: ['Blue'],
        },
      }),
    ).toEqual([{ at_ms: 1000, kind: 'takes', text: 'Draft · Blue has range' }]);
  });
});

describe('Rule edits', () => {
  const form = {
    instructions: 'Winner?',
    threshold: 0.7,
    cooldown_s: 600,
    pack: null,
    enabled: true,
  };
  it('omits unchanged enablement so a wording save preserves a concurrent disable', () => {
    expect(ruleEditBody(form, true)).toEqual({
      instructions: 'Winner?',
      threshold: 0.7,
      cooldown_s: 600,
      pack: null,
    });
  });
  it('sends an explicit checkbox change and enablement for a new Rule', () => {
    expect(ruleEditBody({ ...form, enabled: false }, true)).toEqual({
      ...form,
      enabled: false,
    });
    expect(ruleEditBody(form, false)).toEqual(form);
    expect(ruleEditBody(form, null)).toEqual(form);
  });
});

it('reads the cursor of a long sorted transcript without spreading arguments', () => {
  const rows = Array.from({ length: 150000 }, (_, i) => speech(i, i));
  expect(transcriptCursor(rows)).toBe(149999);
  expect(transcriptCursor([])).toBe(-1);
});
