import { z } from 'zod';

const decimal = z.union([z.number(), z.string()]).pipe(z.coerce.number());
export const sessionSchema = z.object({
  id: z.number(),
  channel: z.string(),
  title: z.string(),
  category: z.string().nullable(),
  started_at: z.string(),
  ended_at: z.string().nullable(),
  mode: z.string().nullable(),
  alert_count: z.number(),
  note_count: z.number(),
});
export type Session = z.infer<typeof sessionSchema>;
export const utteranceSchema = z.object({
  id: z.number(),
  audio_start_ms: z.number(),
  audio_end_ms: z.number(),
  spoken_at: z.string(),
  text: z.string(),
  transcriber: z.string(),
});
export type Utterance = z.infer<typeof utteranceSchema>;
export const noteSchema = z.object({
  id: z.number(),
  start_ms: z.number(),
  end_ms: z.number(),
  pack: z.string().nullable(),
  mode: z.string().nullable(),
  status: z.enum([
    'pending',
    'ok',
    'empty',
    'invalid',
    'failed',
    'over_budget',
  ]),
  body: z.record(z.string(), z.json()).nullable(),
  schema_name: z.string(),
  schema_version: z.number(),
});
export type Note = z.infer<typeof noteSchema>;
export const ruleSchema = z.object({
  id: z.number(),
  name: z.string(),
  instructions: z.string(),
  threshold: decimal,
  pack: z.string().nullable(),
  cooldown_s: z.number(),
  enabled: z.boolean(),
});
export type Rule = z.infer<typeof ruleSchema>;
export const reviewSchema = z.object({
  id: z.number(),
  rule: z.string(),
  question: z.string(),
  probability: decimal,
  threshold: decimal,
  passed: z.boolean(),
  session_id: z.number(),
  channel: z.string(),
  start_ms: z.number(),
  new: z.string(),
  earlier: z.string(),
});
export type Review = z.infer<typeof reviewSchema>;
export const alertSchema = z.object({
  id: z.number(),
  decision_id: z.number(),
  rule: z.string(),
  outcome: z.string(),
  said_at: z.string().nullable(),
  start_ms: z.number(),
  text: z.string(),
  label: z.boolean().nullable(),
});
export type Alert = z.infer<typeof alertSchema>;
export const passSchema = z.object({
  id: z.number(),
  rule: z.string(),
  question: z.string(),
  probability: decimal,
  threshold: decimal,
  start_ms: z.number(),
});
export type GatePass = z.infer<typeof passSchema>;
const trialSchema = z.object({
  session_id: z.number(),
  start_ms: z.number(),
  new: z.string(),
  probability: z.number().nullable(),
  error: z.string().nullable(),
});
export const reportSchema = z.object({
  question: z.string(),
  threshold: decimal,
  trials: z.array(trialSchema),
  scored: z.number(),
  passed: z.number(),
  failed: z.number(),
  pass_rate: z.number(),
  histogram: z.array(z.number()),
  highest: z.array(trialSchema),
  lowest: z.array(trialSchema),
  report: z.string(),
});
export type Report = z.infer<typeof reportSchema>;
export const testJobSchema = z.discriminatedUnion('status', [
  z.object({
    status: z.literal('running'),
    id: z.string(),
    completed: z.number(),
    total: z.number(),
  }),
  z.object({
    status: z.literal('completed'),
    id: z.string(),
    completed: z.number(),
    total: z.number(),
    report: reportSchema,
  }),
  z.object({
    status: z.literal('failed'),
    id: z.string(),
    completed: z.number(),
    total: z.number(),
    error: z.string(),
  }),
]);
export const transcriptCursorSchema = z.object({
  id: z.number().int().nonnegative(),
});
export const reconcileSchema = z.object({ after_ms: z.number().int().min(-1) });

export async function request(
  path: string,
  options: RequestInit = {},
): Promise<unknown> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    const value: unknown = await response.json().catch(() => null);
    const parsed = z.object({ detail: z.string() }).safeParse(value);
    throw new Error(
      parsed.success
        ? parsed.data.detail
        : `Request failed (${response.status})`,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}
export async function read<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  return schema.parse(await request(path));
}
export function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed. Try again.';
}
