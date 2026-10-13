import type { Review, Utterance, Note } from './api';
import { z } from 'zod';

export function clock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}
export function date(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}
export function atBottom(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
): boolean {
  return scrollHeight - scrollTop - clientHeight <= 80;
}
export function mergeTranscript(
  current: Utterance[],
  incoming: Utterance[],
): Utterance[] {
  const rows = new Map(current.map((row) => [row.id, row]));
  for (const row of incoming) rows.set(row.id, row);
  return [...rows.values()].sort(
    (a, b) => a.audio_start_ms - b.audio_start_ms || a.id - b.id,
  );
}
export function removeCard(queue: Review[], id: number): Review[] {
  return queue.filter((card) => card.id !== id);
}
export function restoreCard(queue: Review[], card: Review): Review[] {
  return [card, ...queue.filter((item) => item.id !== card.id)];
}
export type NoteItem = { at_ms: number; kind: string; text: string };
const timestamped = z
  .object({ at_ms: z.number().int().nonnegative() })
  .catchall(z.json());
export function noteItems(note: Note): NoteItem[] {
  const items: NoteItem[] = [];
  for (const [kind, values] of Object.entries(note.body ?? {})) {
    if (!Array.isArray(values)) continue;
    for (const value of values) {
      const parsed = timestamped.safeParse(value);
      if (!parsed.success) continue;
      items.push({
        at_ms: parsed.data.at_ms,
        kind,
        text: Object.entries(parsed.data)
          .filter(([key]) => key !== 'at_ms')
          .map(([, text]) =>
            typeof text === 'string' ? text : JSON.stringify(text),
          )
          .join(' · '),
      });
    }
  }
  return items.sort((a, b) => a.at_ms - b.at_ms);
}
