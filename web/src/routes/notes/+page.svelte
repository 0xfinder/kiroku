<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { z } from 'zod';
  import {
    read,
    message,
    sessionSchema,
    noteSchema,
    type Session,
    type Note,
  } from '#lib/api.ts';
  import NoteCards from '#lib/NoteCards.svelte';
  import { date } from '#lib/helpers.ts';
  let sessions = $state<Session[]>([]);
  let notes = $state<Note[]>([]);
  let selected = $state(Number(page.url.searchParams.get('session') ?? 0));
  let error = $state('');
  let loading = $state(true);
  let disposed = false;
  async function load() {
    const id = selected;
    try {
      const result = await read(`/sessions/${id}/notes`, z.array(noteSchema));
      if (!disposed && selected === id) {
        notes = result;
        error = '';
      }
    } catch (e) {
      error = message(e);
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void (async () => {
      try {
        sessions = await read('/sessions?limit=100', z.array(sessionSchema));
        if (!selected) selected = sessions[0]?.id ?? 0;
        if (selected) await load();
        else loading = false;
      } catch (e) {
        error = message(e);
        loading = false;
      }
    })();
    const timer = setInterval(() => {
      if (selected) void load();
    }, 30000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  });
</script>

<div class="page-heading">
  <span class="eyebrow">A little less noise</span>
  <h1>Notes timeline</h1>
  <p class="subtitle">
    The story of a session, with a path back to every moment.
  </p>
</div>
{#if sessions.length}<label for="session">Session</label><select
    id="session"
    bind:value={selected}
    onchange={() => {
      notes = [];
      loading = true;
      void load();
    }}
    style="margin-bottom:24px"
    >{#each sessions as session}<option value={session.id}
        >{session.channel} · {date(session.started_at)} · {session.title}</option
      >{/each}</select
  >{/if}
{#if error}<p class="error" role="alert">
    {error}<button onclick={load}>Retry</button>
  </p>{/if}
{#if loading}<div class="empty">Loading Notes…</div>{:else if !selected}<div
    class="empty"
  >
    Choose a session when Kiroku starts listening.
  </div>{:else}<NoteCards {notes} sessionId={selected} />{/if}
