<script lang="ts">
  import { onMount } from 'svelte';
  import { z } from 'zod';
  import { read, sessionSchema, message, type Session } from '#lib/api.ts';
  import { date } from '#lib/helpers.ts';
  let sessions = $state<Session[]>([]);
  let error = $state('');
  let loading = $state(true);
  let filter = $state<'all' | 'live' | 'recent'>('all');
  let shown = $derived(
    sessions.filter(
      (s) =>
        filter === 'all' || (filter === 'live' ? !s.ended_at : !!s.ended_at),
    ),
  );
  async function refresh() {
    try {
      sessions = await read('/sessions?limit=50', z.array(sessionSchema));
      error = '';
    } catch (e) {
      error = message(e);
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  });
</script>

<div class="page-heading">
  <span class="eyebrow">Your listening room</span>
  <h1>Live & recent sessions</h1>
  <p class="subtitle">
    Follow the conversation. Return to the moments that matter.
  </p>
</div>
<div class="filters" aria-label="Filter sessions">
  <button class:selected={filter === 'all'} onclick={() => (filter = 'all')}
    >All sessions</button
  ><button class:selected={filter === 'live'} onclick={() => (filter = 'live')}
    >Live now</button
  ><button
    class:selected={filter === 'recent'}
    onclick={() => (filter = 'recent')}>Recent</button
  >
</div>
{#if error}<p class="error" role="alert">
    {error} <button onclick={refresh}>Retry</button>
  </p>{/if}
{#if loading}<div class="empty">
    Loading sessions…
  </div>{:else if shown.length === 0}<div class="empty">
    <h2>
      {filter === 'live'
        ? 'No one is live right now'
        : 'A quiet notebook, for now'}
    </h2>
    <p>Sessions appear here when Kiroku starts listening.</p>
  </div>{:else}<div class="grid">
    {#each shown as session}<a
        class="card card-link"
        href={`/session/${session.id}`}
        ><div class="row between">
          <span class="channel">{session.channel}</span><span
            class="chip"
            class:live={!session.ended_at}
            >{#if !session.ended_at}<span class="live-dot"
              ></span>Live{:else}Ended{/if}</span
          >
        </div>
        <h2 style="margin-top:18px">{session.title || 'Untitled stream'}</h2>
        <p class="muted">
          {session.category ?? 'Unknown category'} · {date(session.started_at)}
        </p>
        <span class="chip"
          >{session.mode?.replaceAll('_', ' ') ?? 'Mode unknown'}</span
        >
        <div class="stats">
          <span><strong>{session.note_count}</strong> Notes</span><span
            ><strong>{session.alert_count}</strong> Alerts</span
          ><span style="margin-left:auto">Open session ↗</span>
        </div></a
      >{/each}
  </div>{/if}
