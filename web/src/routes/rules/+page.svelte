<script lang="ts">
  import { onMount } from 'svelte';
  import { z } from 'zod';
  import {
    read,
    request,
    message,
    ruleSchema,
    testJobSchema,
    type Rule,
    type Report,
  } from '#lib/api.ts';
  import { clock, ruleEditBody } from '#lib/helpers.ts';
  let rules = $state<Rule[]>([]);
  let selected = $state<string | null>(null);
  let name = $state('');
  let instructions = $state('');
  let threshold = $state(0.7);
  let cooldown = $state(600);
  let pack = $state('');
  let enabled = $state(true);
  let loadedEnabled: boolean | null = null;
  let testId = $state<string | null>(null);
  let progress = $state('');
  let destroyed = false;
  let error = $state('');
  let notice = $state('');
  let loading = $state(true);
  let busy = $state(false);
  let testing = $state(false);
  let confirm = $state(false);
  let last = $state(40);
  let report = $state<Report | null>(null);
  let editing = $state(false);
  function choose(rule: Rule | null) {
    selected = rule?.name ?? null;
    name = rule?.name ?? '';
    instructions = rule?.instructions ?? '';
    threshold = rule?.threshold ?? 0.7;
    cooldown = rule?.cooldown_s ?? 600;
    pack = rule?.pack ?? '';
    enabled = rule?.enabled ?? true;
    loadedEnabled = rule?.enabled ?? null;
    report = null;
    error = '';
    notice = '';
    editing = true;
    confirm = false;
  }
  async function refresh() {
    try {
      rules = await read('/rules', z.array(ruleSchema));
      error = '';
    } catch (e) {
      error = message(e);
    } finally {
      loading = false;
    }
  }
  async function save(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    error = '';
    try {
      const body = ruleEditBody(
        {
          instructions,
          threshold,
          cooldown_s: cooldown,
          pack: pack || null,
          enabled,
        },
        loadedEnabled,
      );
      const saved = ruleSchema.parse(
        await request(selected ? `/rules/${selected}` : '/rules', {
          method: selected ? 'PATCH' : 'POST',
          body: JSON.stringify(selected ? body : { name, ...body }),
        }),
      );
      await refresh();
      choose(saved);
      notice = 'Rule saved. Future Windows use this wording.';
    } catch (e) {
      error = message(e);
    } finally {
      busy = false;
    }
  }
  async function test() {
    confirm = false;
    testing = true;
    error = '';
    notice = '';
    progress = '';
    report = null;
    try {
      const job = testJobSchema.parse(
        await request(`/rules/${selected}/test`, {
          method: 'POST',
          body: JSON.stringify({ instructions, threshold, last }),
        }),
      );
      testId = job.id;
      await pollTest();
    } catch (e) {
      error = message(e);
    } finally {
      testing = false;
    }
  }
  async function pollTest() {
    testing = true;
    error = '';
    try {
      while (!destroyed && testId) {
        const job = await read(`/rule-tests/${testId}`, testJobSchema);
        progress = `${job.completed} of ${job.total} Windows tested`;
        if (job.status === 'completed') {
          report = job.report;
          testId = null;
          break;
        }
        if (job.status === 'failed') {
          testId = null;
          throw new Error(job.error);
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } catch (e) {
      if (!destroyed) error = message(e);
    } finally {
      testing = false;
    }
  }
  onMount(() => {
    void refresh();
    return () => {
      destroyed = true;
    };
  });
</script>

<div class="page-heading row between">
  <div>
    <span class="eyebrow">Keep an ear out</span>
    <h1>Your Rules</h1>
    <p class="subtitle">Tell Kiroku which moments deserve your attention.</p>
  </div>
  <button
    class="primary"
    onclick={() => choose(null)}
    disabled={busy || testing}>＋ New Rule</button
  >
</div>
{#if error}<p class="error" role="alert">{error}</p>{/if}{#if notice}<p
    class="notice"
    role="status"
  >
    {notice}
  </p>{/if}
{#if testing}<p role="status" class="notice">
    {progress || 'Starting test…'}
  </p>{/if}
{#if testId && !testing}<button onclick={pollTest}>Resume test results</button
  >{/if}
<div class="rule-layout">
  <section class="stack" aria-label="Rules">
    {#if loading}<div class="empty">
        Loading Rules…
      </div>{:else if rules.length === 0}<div class="empty">
        No Rules yet. Start with a concrete event you care about.
      </div>{/if}{#each rules as rule}<button
        class="rule-button"
        class:selected={selected === rule.name}
        disabled={busy || testing}
        onclick={() => choose(rule)}
        ><div class="row between">
          <strong>{rule.name.replaceAll('_', ' ')}</strong><span
            class="chip"
            class:live={rule.enabled}
            >{rule.enabled ? 'Enabled' : 'Disabled'}</span
          >
        </div>
        <small
          >{Math.round(rule.threshold * 100)}% threshold · {rule.pack ??
            'Any Pack'}</small
        ></button
      >{/each}
  </section>
  <section>
    {#if editing}<div class="card">
        <h2>{selected ? 'Edit Rule' : 'New Rule'}</h2>
        <form onsubmit={save}>
          <label for="name">Name</label><input
            id="name"
            bind:value={name}
            disabled={selected !== null || busy || testing}
            required
            pattern={'[a-z][a-z0-9_]{0,62}'}
            placeholder="predicts_winner"
          /><label for="instructions">Instructions</label><textarea
            id="instructions"
            bind:value={instructions}
            disabled={busy || testing}
            required
            maxlength="2000"
            placeholder="Does the streamer predict which team will win the current game?"
          ></textarea>
          <p class="muted" style="font-size:11px;margin-top:6px">
            Name a concrete event. Questions work best when they refer to what
            was said.
          </p>
          <div class="form-grid">
            <div>
              <label for="threshold">Threshold</label><input
                id="threshold"
                type="number"
                bind:value={threshold}
                disabled={busy || testing}
                min="0.01"
                max="1"
                step="0.01"
                required
              />
            </div>
            <div>
              <label for="cooldown">Cooldown (seconds)</label><input
                id="cooldown"
                type="number"
                bind:value={cooldown}
                disabled={busy || testing}
                min="0"
                step="1"
                required
              />
            </div>
            <div>
              <label for="pack">Pack</label><select
                id="pack"
                bind:value={pack}
                disabled={busy || testing}
                ><option value="">Any Pack</option><option value="league"
                  >League</option
                ><option value="dota">Dota</option></select
              >
            </div>
            <label class="checkbox"
              ><input
                type="checkbox"
                bind:checked={enabled}
                disabled={busy || testing}
              />Enabled</label
            >
          </div>
          <div class="row" style="margin-top:22px">
            <button class="primary" type="submit" disabled={busy || testing}
              >{busy ? 'Saving…' : 'Save Rule'}</button
            >
          </div>
        </form>
        {#if selected}<div
            style="border-top:1px solid var(--line);margin-top:24px;padding-top:18px"
          >
            <h3>Try the wording</h3>
            <p class="muted">
              Test these instructions against stored Windows before you save.
              Past decisions stay unchanged.
            </p>
            <label for="last">Recent Windows (1–100)</label><input
              id="last"
              type="number"
              min="1"
              max="100"
              step="1"
              bind:value={last}
              disabled={testing}
            /><button
              style="margin-top:14px;width:100%"
              disabled={busy ||
                testing ||
                !Number.isInteger(last) ||
                last < 1 ||
                last > 100}
              onclick={() => (confirm = true)}
              >{testing ? 'Testing…' : 'Test against recent windows'}</button
            >{#if confirm}<div
                class="notice"
                style="display:block"
                role="alertdialog"
                aria-label="Confirm paid Rule test"
              >
                <p>
                  This makes up to {last} paid Jev calls. Estimated cost: ${(
                    last * 0.00003
                  ).toFixed(5)} USD. Actual cost depends on text length and counts
                  against your monthly cap.
                </p>
                <div class="row">
                  <button class="primary" onclick={test}
                    >Confirm paid test</button
                  ><button onclick={() => (confirm = false)}>Cancel</button>
                </div>
              </div>{/if}
          </div>{/if}
      </div>{:else}<div class="empty">
        <h2>What are you listening for?</h2>
        <p>Choose a Rule to adjust its wording, or create a new one.</p>
      </div>{/if}
    {#if report}<article class="card" style="margin-top:18px">
        <span class="eyebrow">Test results</span>
        <h2 style="margin-top:10px">
          {report.passed} of {report.scored} Windows pass
        </h2>
        <p class="muted">
          {Math.round(report.pass_rate * 100)}% pass rate at {Math.round(
            report.threshold * 100,
          )}% · {report.failed} not scored
        </p>
        {#if report.failed}
          <details open>
            <summary>Not scored ({report.failed})</summary>
            {#each report.trials
              .filter((trial) => trial.error !== null)
              .slice(0, 3) as trial}
              <p class="muted">
                Session {trial.session_id} · {clock(trial.start_ms)}: {trial.error}
              </p>
            {/each}
          </details>
        {/if}
        <div class="histogram" aria-label="Probability histogram">
          {#each report.histogram as count, i}<div class="bucket">
              <span>{count}</span>
              <div
                class="bar"
                style:height={`${(count / Math.max(1, ...report.histogram)) * 80}%`}
              ></div>
              <small>{i / 10}–{(i + 1) / 10}</small>
            </div>{/each}
        </div>
        {#each [{ title: 'Highest', trials: report.highest }, { title: 'Lowest', trials: report.lowest }] as group}<h3
            style="margin-top:22px"
          >
            {group.title}
          </h3>
          {#each group.trials as trial}<div class="example">
              <a href={`/session/${trial.session_id}?at=${trial.start_ms}`}
                >{clock(trial.start_ms)} ↗</a
              >
              · {Math.round((trial.probability ?? 0) * 100)}%
              <p style="margin-top:8px">{trial.new}</p>
            </div>{/each}{/each}
      </article>{/if}
  </section>
</div>
