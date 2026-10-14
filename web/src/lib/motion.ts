import { cubicOut } from 'svelte/easing';
import { fly } from 'svelte/transition';
import type { Utterance } from './api';

export function reducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

// Durations mirror the CSS tokens; read the preference at use time so changes apply live.
export function motionDuration(
  duration = 300,
  reduced = reducedMotion(),
): number {
  return reduced ? 0 : duration;
}

export function enter(node: Element, enabled = true) {
  const duration = enabled ? motionDuration() : 0;
  if (duration === 0) return { duration: 0 };
  return fly(node, {
    duration,
    y: 6,
    opacity: 0.35,
    easing: cubicOut,
  });
}

export function fadeOptions() {
  return { duration: motionDuration(150), easing: cubicOut };
}

export function flipOptions() {
  return { duration: motionDuration(220), easing: cubicOut };
}

export function scrollBehavior(smooth: boolean): ScrollBehavior {
  return smooth && !reducedMotion() ? 'smooth' : 'instant';
}

export type TranscriptSource = 'snapshot' | 'reconcile' | 'live';
export function transcriptArrivals() {
  const seen = new Set<number>();
  let latest = -1;
  return {
    accept(
      incoming: Pick<Utterance, 'id' | 'audio_start_ms'>[],
      source: TranscriptSource,
    ): Set<number> {
      const arrivals = new Set<number>();
      for (const row of incoming) {
        if (
          source === 'live' &&
          !seen.has(row.id) &&
          row.audio_start_ms >= latest
        )
          arrivals.add(row.id);
        seen.add(row.id);
      }
      for (const row of incoming) latest = Math.max(latest, row.audio_start_ms);
      return arrivals;
    },
  };
}
