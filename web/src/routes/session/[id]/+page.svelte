<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { fade } from 'svelte/transition';
  import {
    enter,
    fadeOptions,
    fadeIn,
    scrollBehavior,
    transcriptArrivals,
  } from '#lib/motion.ts';
  import Skeleton from '#lib/Skeleton.svelte';
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
  let transcriptError = $state('');
  let refreshError = $state('');
  let error = $derived(transcriptError || refreshError);
  let connection = $state('Connecting');
  let box = $state<HTMLDivElement>();
  let follow = $state(true);
  let destroyed = false;
  let loading = $state(true);
  let scrolling = false;
  let scrollTimer: ReturnType<typeof setTimeout> | undefined;
  let stream: ReturnType<typeof transcriptStream> | undefined;
  let starting = false;
  let liveReady = false;
  const arrivals = transcriptArrivals();
  let enterIds = $state(new Set<number>());
  let reconciling: Promise<void> | null = null;
  const seenEvents = new Set<string>();
  let enterEventKeys = $state(new Set<string>());
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
  type TranscriptItem =
    | { kind: 'utterance'; key: string; row: Utterance }
    | { kind: 'event'; key: string; event: (typeof inline)[number] };
  let items = $derived.by(() => {
    const result: TranscriptItem[] = [];
    let eventIndex = 0;
    for (const row of rows) {
      while (
        eventIndex < inline.length &&
        inline[eventIndex].at <= row.audio_start_ms
      ) {
        const event = inline[eventIndex++];
        result.push({ kind: 'event', key: `e${event.key}`, event });
      }
      result.push({ kind: 'utterance', key: `u${row.id}`, row });
    }
    for (; eventIndex < inline.length; eventIndex++) {
      const event = inline[eventIndex];
      result.push({ kind: 'event', key: `e${event.key}`, event });
    }
    return result;
  });
  function stopScrolling() {
    clearTimeout(scrollTimer);
    scrolling = false;
  }
  function updateFollow() {
    if (box)
      follow = atBottom(box.scrollTop, box.clientHeight, box.scrollHeight);
  }
  function scrolled() {
    if (box && atBottom(box.scrollTop, box.clientHeight, box.scrollHeight))
      stopScrolling();
    if (!scrolling) updateFollow();
  }
  async function scrollLive(smooth = false) {
    await tick();
    if (box && follow && !destroyed) {
      stopScrolling();
      const behavior = scrollBehavior(smooth);
      scrolling =
        behavior === 'smooth' &&
        box.scrollHeight - box.clientHeight > box.scrollTop + 1;
      if (scrolling)
        scrollTimer = setTimeout(() => {
          stopScrolling();
          updateFollow();
        }, 600);
      box.scrollTo({ top: box.scrollHeight, behavior });
    }
  }
  function readerIntent(node: HTMLDivElement) {
    node.tabIndex = 0;
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    const interrupt = () => {
      if (!scrolling) return;
      stopScrolling();
      node.scrollTo({ top: node.scrollTop, behavior: 'instant' });
      updateFollow();
    };
    for (const event of events)
      node.addEventListener(event, interrupt, { passive: true });
    return {
      destroy() {
        for (const event of events) node.removeEventListener(event, interrupt);
      },
    };
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
            arrivals.accept(
              incoming,
              throughId === null ? 'reconcile' : 'snapshot',
            );
            rows = mergeTranscript(rows, incoming);
            transcriptError = '';
            await scrollLive();
          }
        } catch (e) {
          if (!destroyed) transcriptError = message(e);
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
        enterEventKeys = new Set(
          inline
            .filter((event) => {
              const unseen = !seenEvents.has(event.key);
              seenEvents.add(event.key);
              return unseen && !loading && liveReady;
            })
            .map((event) => event.key),
        );
        refreshError = '';
        await scrollLive();
      }
    } catch (e) {
      if (!destroyed) refreshError = message(e);
    }
  }
  async function start() {
    follow = at < 0;
    let cursor: z.infer<typeof transcriptCursorSchema>;
    try {
      cursor = await read(
        `/sessions/${id}/transcript/cursor`,
        transcriptCursorSchema,
      );
      await Promise.all([reconcile(-1, cursor.id), refresh()]);
    } finally {
      if (!destroyed) loading = false;
    }
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
    const openedStream = transcriptStream({
      url: `/api/sessions/${id}/transcript/stream?after_ms=${after}&after_id=${cursor.id}`,
      status: (value) => {
        connection = value;
        if (value !== 'Connected') liveReady = false;
      },
      opened: () => {
        if (opened) {
          liveReady = false;
          void reconcile().then(() => {
            liveReady = connection === 'Connected' && !destroyed;
          });
        } else liveReady = true;
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
          enterIds = arrivals.accept(
            incoming,
            liveReady ? 'live' : 'reconcile',
          );
          rows = mergeTranscript(rows, incoming);
          void scrollLive(true);
        } catch (e) {
          transcriptError = message(e);
        }
      },
      reconcile: (event) => {
        try {
          const hint = reconcileSchema.parse(JSON.parse(event.data));
          void reconcile(hint.after_ms);
        } catch (e) {
          transcriptError = message(e);
        }
      },
    });
    return openedStream;
  }
  async function retry() {
    if (starting) return;
    if (stream) {
      await Promise.all([refresh(), reconcile()]);
      return;
    }
    starting = true;
    await start()
      .then((value) => {
        if (destroyed) value?.close();
        else stream = value;
      })
      .catch((e) => {
        if (!destroyed) transcriptError = message(e);
      })
      .finally(() => {
        starting = false;
      });
  }
  onMount(() => {
    void retry();
    const timer = setInterval(refresh, 30000);
    return () => {
      destroyed = true;
      stream?.close();
      clearInterval(timer);
      stopScrolling();
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
    <button onclick={retry}>Retry</button>
  </p>{/if}
<div class="session-grid">
  <section>
    <div class="section-head">
      <h2>Transcript</h2>
      <span class="muted connection-status" role="status">
        {#key connection}<span in:fade={fadeOptions()}>{connection}</span>{/key}
      </span>
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
        onscroll={scrolled}
        onscrollend={() => {
          stopScrolling();
          updateFollow();
        }}
        use:readerIntent
        role="region"
        aria-label="Live transcript"
        data-testid="transcript"
      >
        {#if loading && rows.length === 0}<Skeleton
            label="Loading transcript…"
          />{:else if rows.length === 0}<div class="empty" style="border:0">
            Waiting for speech…
          </div>{/if}
        {#each items as item (item.key)}
          {#if item.kind === 'event'}
            <div
              class="inline-event"
              in:fadeIn|global={enterEventKeys.has(item.event.key)}
            >
              {clock(item.event.at)} · {item.event.text}
            </div>
          {:else}
            <article
              class="utterance"
              id={item.key}
              in:enter|global={enterIds.has(item.row.id)}
            >
              <time datetime={item.row.spoken_at}
                >{clock(item.row.audio_start_ms)}</time
              >
              <p>{item.row.text}</p>
            </article>
          {/if}
        {/each}
      </div>
      {#if !follow}<button
          class="jump-live"
          onclick={() => {
            follow = true;
            void scrollLive(true);
          }}>↓ Jump to live</button
        >{/if}
    </div>
  </section>
  <aside>
    <div class="section-head">
      <h2>Latest Notes</h2>
      <a class="muted" href={`/notes?session=${id}`}>View timeline ↗</a>
    </div>
    {#if latest}{#key latest.id}<article class="card note-card" in:enter>
          <div class="row between">
            <span class="eyebrow"
              >{clock(latest.start_ms)} — {clock(latest.end_ms)}</span
            ><span class="chip"
              >{latest.mode?.replaceAll('_', ' ') ?? 'unknown'}</span
            >
          </div>
          <p style="margin-top:16px">
            {typeof latest.body?.summary === 'string'
              ? latest.body.summary
              : ''}
          </p>
          <a href={`/notes?session=${id}`} class="muted"
            >Read timestamped items ↗</a
          >
        </article>{/key}{:else}<div class="empty">
        Notes will appear here.
      </div>{/if}
    <div class="notice">
      The transcript follows live while you’re at the bottom. Scroll up to
      pause.
    </div>
  </aside>
</div>
