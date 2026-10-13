<script lang="ts">
  import { onMount } from 'svelte';
  import { z } from 'zod';
  import {
    read,
    request,
    message,
    reviewSchema,
    type Review,
  } from '#lib/api.ts';
  import { clock, removeCard, restoreCard } from '#lib/helpers.ts';
  type Vote =
    | { kind: 'idle' }
    | { kind: 'saving' | 'saved' | 'undoing'; card: Review; applies: boolean };
  let queue = $state<Review[]>([]);
  let loading = $state(true);
  let error = $state('');
  let vote = $state<Vote>({ kind: 'idle' });
  let reviewed = $state(0);
  let current = $derived(queue[0]);
  let busy = $derived(vote.kind === 'saving' || vote.kind === 'undoing');
  async function refresh() {
    try {
      queue = await read('/review?limit=100', z.array(reviewSchema));
      error = '';
    } catch (e) {
      error = message(e);
    } finally {
      loading = false;
    }
  }
  async function label(card: Review, applies: boolean) {
    queue = removeCard(queue, card.id);
    vote = { kind: 'saving', card, applies };
    error = '';
    try {
      await request(`/review/${card.id}`, {
        method: 'POST',
        body: JSON.stringify({ applies }),
      });
      vote = { kind: 'saved', card, applies };
      reviewed++;
    } catch (e) {
      queue = restoreCard(queue, card);
      vote = { kind: 'idle' };
      error = message(e);
    }
  }
  async function undo() {
    if (vote.kind !== 'saved') return;
    const { card, applies } = vote;
    vote = { kind: 'undoing', card, applies };
    try {
      await request(`/review/${card.id}`, { method: 'DELETE' });
      queue = restoreCard(queue, card);
      vote = { kind: 'idle' };
      reviewed--;
    } catch (e) {
      vote = { kind: 'saved', card, applies };
      error = message(e);
    }
  }
  onMount(() => {
    void refresh();
  });
</script>

<div class="review-wrap">
  <div class="page-heading">
    <span class="eyebrow">Teach it what matters</span>
    <h1>A second look</h1>
    <p class="subtitle">
      These decisions sit between 30% and 70%. Does the Rule apply to what was
      said?
    </p>
  </div>
  <div class="section-head">
    <span class="muted">{queue.length} in this batch</span><span class="chip"
      >{reviewed} reviewed</span
    >
  </div>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if vote.kind !== 'idle'}<div class="notice" role="status">
      <span
        >{vote.kind === 'saving'
          ? 'Saving verdict…'
          : vote.kind === 'undoing'
            ? 'Undoing…'
            : `Labelled ${vote.applies ? 'yes' : 'no'}.`}</span
      ><button onclick={undo} disabled={vote.kind !== 'saved'}>Undo</button>
    </div>{/if}
  {#if loading}<div class="empty">
      Loading Review queue…
    </div>{:else if current}<article class="card" data-testid="review-card">
      <div class="row between">
        <span class="channel">{current.rule.replaceAll('_', ' ')}</span><a
          class="chip"
          href={`/session/${current.session_id}?at=${current.start_ms}`}
          >{current.channel} · {clock(current.start_ms)} ↗</a
        >
      </div>
      <p class="review-question" style="margin-top:20px">{current.question}</p>
      <blockquote class="excerpt" style="margin-left:0;margin-right:0">
        {current.new}
      </blockquote>
      {#if current.earlier}<details>
          <summary>Earlier context</summary>
          <p>{current.earlier}</p>
        </details>{/if}
      <div class="row between">
        <span class="muted" style="font-size:12px"
          >Probability <strong style="color:var(--ink)"
            >{Math.round(current.probability * 100)}%</strong
          ></span
        ><span class="muted" style="font-size:12px"
          >Threshold {Math.round(current.threshold * 100)}%</span
        >
      </div>
      <div
        class="probability"
        role="meter"
        aria-label="Gate probability"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={current.probability * 100}
      >
        <div class="fill" style:width={`${current.probability * 100}%`}></div>
        <div class="threshold" style:left={`${current.threshold * 100}%`}></div>
      </div>
      <p class="muted" style="font-size:11px">
        The Gate {current.passed ? 'passed' : 'held'} this Window. Your label judges
        the saved question above.
      </p>
      <div class="vote-controls">
        <button class="yes" disabled={busy} onclick={() => label(current, true)}
          >👍 Yes, it applies</button
        ><button disabled={busy} onclick={() => label(current, false)}
          >👎 No, it doesn’t</button
        >
      </div>
    </article>{:else}<div class="empty">
      <span style="font-size:30px;color:var(--accent)">✓</span>
      <h2 style="margin-top:14px">You’re all caught up</h2>
      <p>No unreviewed decisions in this batch.</p>
      <button onclick={refresh} disabled={busy}>Check for more</button>
    </div>{/if}
</div>
