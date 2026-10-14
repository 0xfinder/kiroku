<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { transcriptStream } from '#lib/stream.ts';
  import { page } from '$app/state';
  import { z } from 'zod';
  import {
    read,
    request,
    message,
    sessionSchema,
    utteranceSchema,
    noteSchema,
    alertSchema,
    passSchema,
    transcriptCursorSchema,
    reconcileSchema,
    type Session,
    type Utterance,
    type Note,
    type Alert,
    type GatePass,
  } from '#lib/api.ts';
  import {
    clock,
    atBottom,
    mergeTranscript,
    transcriptCursor,
  } from '#lib/helpers.ts';
  let session = $state<Session | null>(null);
  let rows = $state<Utterance[]>([]);
  let notes = $state<Note[]>([]);
  let alerts = $state<Alert[]>([]);
  let passes = $state<GatePass[]>([]);
  let error = $state('');
  let connection = $state('Connecting');
  let box = $state<HTMLDivElement>();
  let follow = $state(true);
  let destroyed = false;
  let reconciling: Promise<void> | null = null;
  let latest = $derived(notes.filter((n) => n.status === 'ok').at(-1));
  const id = Number(page.params.id);
  const at = Number(page.url.searchParams.get('at') ?? -1);
  let inline = $derived(
    [
      ...passes.map((p) => ({
        key: `p${p.id}`,
        at: p.start_ms,
        text: `Gate pass · ${p.rule} · ${Math.round(p.probability * 100)}%`,
      })),
      ...alerts.map((a) => ({
        key: `a${a.id}`,
        at: a.start_ms,
        text: `Alert · ${a.outcome} · ${a.text || a.rule}`,
      })),
    ].sort((a, b) => a.at - b.at),
  );
  async function scrollLive() {
    await tick();
    if (box && follow) box.scrollTop = box.scrollHeight;
  }
  let pendingAfter: number | null = null;
  async function reconcile(afterMs = -1, throughId: number | null = null) {
    pendingAfter =
      pendingAfter === null ? afterMs : Math.min(pendingAfter, afterMs);
    if (reconciling) return reconciling;
    reconciling = (async () => {
      while (pendingAfter !== null && !destroyed) {
        let after = pendingAfter;
        pendingAfter = null;
        try {
          let incoming: Utterance[] = [];
          while (!destroyed) {
            const chunk = await read(
              `/sessions/${id}/transcript?after_ms=${after}&limit=500${throughId === null ? '' : `&through_id=${throughId}`}`,
              z.array(utteranceSchema),
            );
            if (chunk.length === 0) break;
            incoming = mergeTranscript(incoming, chunk);
            after = transcriptCursor(chunk);
          }
          if (!destroyed) {
            rows = mergeTranscript(rows, incoming);
            await scrollLive();
          }
        } catch (e) {
          if (!destroyed) error = message(e);
        }
      }
    })();
    try {
      await reconciling;
    } finally {
      reconciling = null;
    }
  }
  async function refresh() {
    try {
      const result = await Promise.all([
        read(`/sessions/${id}`, sessionSchema),
        read(`/sessions/${id}/notes`, z.array(noteSchema)),
        read(`/sessions/${id}/alerts`, z.array(alertSchema)),
        read(`/sessions/${id}/passes`, z.array(passSchema)),
      ]);
      if (!destroyed) {
        [session, notes, alerts, passes] = result;
        error = '';
        await scrollLive();
      }
    } catch (e) {
      if (!destroyed) error = message(e);
    }
  }
  async function start() {
    follow = at < 0;
    const cursor = await read(
      `/sessions/${id}/transcript/cursor`,
      transcriptCursorSchema,
    );
    await Promise.all([reconcile(-1, cursor.id), refresh()]);
    if (destroyed) return;
    if (at >= 0) {
      await tick();
      const row = rows.find((r) => r.audio_start_ms >= at) ?? rows.at(-1);
      if (row)
        document
          .getElementById(`u${row.id}`)
          ?.scrollIntoView({ block: 'center' });
    }
    const after = transcriptCursor(rows);
    let opened = false;
    const stream = transcriptStream({
      url: `/api/sessions/${id}/transcript/stream?after_ms=${after}&after_id=${cursor.id}`,
      status: (value) => {
        connection = value;
      },
      opened: () => {
        if (opened) void reconcile();
        opened = true;
      },
      failed: () => {
        if (!destroyed) void request('/me').catch(() => {});
      },
      transcript: (event) => {
        try {
          const incoming = z
            .array(utteranceSchema)
            .parse(JSON.parse(event.data));
          rows = mergeTranscript(rows, incoming);
          void scrollLive();
        } catch (e) {
          error = message(e);
        }
      },
      reconcile: (event) => {
        try {
          const hint = reconcileSchema.parse(JSON.parse(event.data));
          void reconcile(hint.after_ms);
        } catch (e) {
          error = message(e);
        }
      },
    });
    return stream;
  }
  onMount(() => {
    let stream: ReturnType<typeof transcriptStream> | undefined;
    void start()
      .then((value) => {
        if (destroyed) value?.close();
        else stream = value;
      })
      .catch((e) => {
        if (!destroyed) error = message(e);
      });
    const timer = setInterval(refresh, 30000);
    return () => {
      destroyed = true;
      stream?.close();
      clearInterval(timer);
    };
  });
</script>

<div class="page-heading">
  <a class="eyebrow" href="/">← Sessions</a>
  <div class="row between">
    <h1>{session?.channel ?? 'Session'}</h1>
    {#if session}<span class="chip" class:live={!session.ended_at}
        >{session.ended_at ? 'Ended' : 'Live'}</span
      >{/if}
  </div>
  <p class="subtitle">{session?.title ?? 'Loading conversation…'}</p>
  <div class="row">
    <span class="chip">{session?.category ?? 'Unknown category'}</span><span
      class="chip">{session?.mode?.replaceAll('_', ' ') ?? 'Mode unknown'}</span
    ><a class="chip" href={`/notes?session=${id}`}>{notes.length} Notes ↗</a>
  </div>
</div>
{#if error}<p class="error" role="alert">
    {error}
    <button
      onclick={() => {
        void refresh();
        void reconcile();
      }}>Retry</button
    >
  </p>{/if}
<div class="session-grid">
  <section>
    <div class="section-head">
      <h2>Transcript</h2>
      <span class="muted" style="font-size:11px">{connection}</span>
    </div>
    <div class="card transcript-card">
      <div class="transcript-header row between">
        <span class="eyebrow">The conversation</span><span
          class="muted"
          style="font-size:11px">{rows.length} utterances</span
        >
      </div>
      <div
        class="transcript"
        bind:this={box}
        onscroll={() => {
          if (box)
            follow = atBottom(
              box.scrollTop,
              box.clientHeight,
              box.scrollHeight,
            );
        }}
        data-testid="transcript"
      >
        {#if rows.length === 0}<div class="empty" style="border:0">
            Waiting for speech…
          </div>{/if}
        {#each rows as row, i (row.id)}
          {#each inline.filter((event) => event.at <= row.audio_start_ms && (i === 0 || event.at > rows[i - 1].audio_start_ms)) as event (event.key)}<div
              class="inline-event"
            >
              {clock(event.at)} · {event.text}
            </div>{/each}
          <article class="utterance" id={`u${row.id}`}>
            <time datetime={row.spoken_at}>{clock(row.audio_start_ms)}</time>
            <p>{row.text}</p>
          </article>
        {/each}
        {#each inline.filter((event) => event.at > (rows.at(-1)?.audio_start_ms ?? -1)) as event (event.key)}<div
            class="inline-event"
          >
            {clock(event.at)} · {event.text}
          </div>{/each}
      </div>
      {#if !follow}<button
          class="jump-live"
          onclick={() => {
            follow = true;
            void scrollLive();
          }}>↓ Jump to live</button
        >{/if}
    </div>
  </section>
  <aside>
    <div class="section-head">
      <h2>Latest Notes</h2>
      <a class="muted" href={`/notes?session=${id}`}>View timeline ↗</a>
    </div>
    {#if latest}<article class="card note-card">
        <div class="row between">
          <span class="eyebrow"
            >{clock(latest.start_ms)} — {clock(latest.end_ms)}</span
          ><span class="chip"
            >{latest.mode?.replaceAll('_', ' ') ?? 'unknown'}</span
          >
        </div>
        <p style="margin-top:16px">
          {typeof latest.body?.summary === 'string' ? latest.body.summary : ''}
        </p>
        <a href={`/notes?session=${id}`} class="muted"
          >Read timestamped items ↗</a
        >
      </article>{:else}<div class="empty">Notes will appear here.</div>{/if}
    <div class="notice">
      The transcript follows live while you’re at the bottom. Scroll up to
      pause.
    </div>
  </aside>
</div>
