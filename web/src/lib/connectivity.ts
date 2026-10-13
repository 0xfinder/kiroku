import { writable } from 'svelte/store';

export const serverAvailable = writable(true);
let failures = 0;

export function connectionFailed() {
  failures++;
  if (!navigator.onLine || failures >= 3) serverAvailable.set(false);
}

export function connectionSucceeded() {
  failures = 0;
  serverAvailable.set(navigator.onLine);
}
