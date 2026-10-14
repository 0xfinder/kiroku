import { writable } from 'svelte/store';
import { read } from '#lib/api.ts';
import { serverAvailable } from '#lib/connectivity.ts';
import { z } from 'zod';
export const meSchema = z.discriminatedUnion('owner', [
  z.object({ owner: z.literal(false) }),
  z.object({ owner: z.literal(true), email: z.string() }),
]);
export const access = writable<z.infer<typeof meSchema> | { owner: null }>({
  owner: null,
});
export const loginAvailable = writable(false);
export const identityError = writable('');

export function monitorAccess() {
  let stopped = false;
  let generation = 0;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(
      () => void update(),
      Math.min(30000, 1000 * 2 ** Math.min(failures, 5)),
    );
  };
  const update = async () => {
    clearTimeout(timer);
    const current = ++generation;
    if (!navigator.onLine) {
      serverAvailable.set(false);
      schedule();
      return;
    }
    try {
      const [me, auth] = await Promise.all([
        read('/me', meSchema),
        read('/auth', z.object({ login: z.boolean() })),
      ]);
      if (stopped || current !== generation) return;
      failures = 0;
      identityError.set('');
      access.set(me);
      loginAvailable.set(auth.login);
    } catch {
      if (stopped || current !== generation) return;
      access.set({ owner: null });
      loginAvailable.set(false);
      failures++;
      if (failures >= 3)
        identityError.set('Unable to check owner login; retrying.');
      schedule();
    }
  };
  access.set({ owner: null });
  loginAvailable.set(false);
  identityError.set('');
  const unsubscribe = serverAvailable.subscribe((available) => {
    if (!available) {
      access.set({ owner: null });
      loginAvailable.set(false);
      schedule();
    }
  });
  const online = () => void update();
  const offline = () => {
    generation++;
    serverAvailable.set(false);
  };
  const visible = () => {
    if (document.visibilityState === 'visible') void update();
  };
  window.addEventListener('online', online);
  window.addEventListener('offline', offline);
  document.addEventListener('visibilitychange', visible);
  void update();
  return {
    retry: online,
    stop() {
      stopped = true;
      generation++;
      clearTimeout(timer);
      unsubscribe();
      window.removeEventListener('online', online);
      window.removeEventListener('offline', offline);
      document.removeEventListener('visibilitychange', visible);
    },
  };
}
