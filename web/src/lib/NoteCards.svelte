<script lang="ts">
  import { flip } from 'svelte/animate';
  import { enter, flipOptions } from './motion';
  import type { Note } from './api';
  import { clock, noteItems } from './helpers';
  let { notes, sessionId }: { notes: Note[]; sessionId: number } = $props();
</script>

{#if notes.length === 0}<div class="empty">
    <h2>No Notes yet</h2>
    <p>Notes collect the conversation in three-minute spans.</p>
  </div>{:else}<div class="stack">
    {#each notes as note (note.id)}<article
        transition:enter
        animate:flip={flipOptions()}
        class="card note-card"
      >
        <div class="row between">
          <span class="span">{clock(note.start_ms)} — {clock(note.end_ms)}</span
          ><span class="chip"
            >{note.mode?.replaceAll('_', ' ') ?? 'Mode unknown'}</span
          >
        </div>
        {#if note.status === 'ok'}<p style="margin-top:16px">
            {typeof note.body?.summary === 'string' ? note.body.summary : ''}
          </p>
          {#each noteItems(note) as item}<div class="note-item">
              <a
                href={`/session/${sessionId}?at=${item.at_ms}`}
                aria-label={`Jump to ${clock(item.at_ms)} in transcript`}
                >{clock(item.at_ms)} ↗</a
              >
              <div>
                <small>{item.kind.replaceAll('_', ' ')}</small>{item.text}
              </div>
            </div>{/each}{:else}<p class="muted" style="margin:16px 0 0">
            {note.status.replaceAll('_', ' ')}
          </p>{/if}
      </article>{/each}
  </div>{/if}
