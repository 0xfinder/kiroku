import { writable } from 'svelte/store';
import { z } from 'zod';
export const meSchema = z.discriminatedUnion('owner', [
  z.object({ owner: z.literal(false) }),
  z.object({ owner: z.literal(true), email: z.string() }),
]);
export const access = writable<z.infer<typeof meSchema>>({ owner: false });
