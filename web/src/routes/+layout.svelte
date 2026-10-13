<script lang="ts">
  import { onMount } from 'svelte';
  import { serverAvailable } from '#lib/connectivity.ts';
  import { z } from 'zod';
  import { access, meSchema } from '#lib/access.ts';
  import { read } from '#lib/api.ts';
  import { page } from '$app/state';
  import './style.css';
  let { children } = $props();
  let deviceOffline = $state(false);
  const offline = $derived(deviceOffline || !$serverAvailable);
  let retry = () => {};
  let login = $state(false);
  onMount(() => {
    let generation = 0;
    const update = (recover = false) => {
      const current = ++generation;
      deviceOffline = !navigator.onLine;
      if (deviceOffline) {
        access.set({ owner: false });
        login = false;
      } else
        void Promise.all([
          read('/me', meSchema),
          read('/auth', z.object({ login: z.boolean() })),
        ])
          .then(([me, auth]) => {
            if (current === generation && !deviceOffline) {
              if (recover) serverAvailable.set(true);
              if (!$serverAvailable) return;
              access.set(me);
              login = auth.login;
            }
          })
          .catch(() => {
            if (current === generation) {
              access.set({ owner: false });
              login = false;
            }
          });
    };
    retry = () => update(true);
    update();
    const online = () => update(true);
    const offlineEvent = () => update();
    window.addEventListener('online', online);
    window.addEventListener('offline', offlineEvent);
    return () => {
      generation++;
      window.removeEventListener('online', online);
      window.removeEventListener('offline', offlineEvent);
    };
  });
  $effect(() => {
    if (offline) {
      access.set({ owner: false });
      login = false;
    }
  });
  const tabs = [
    { href: '/', label: 'Live', symbol: '◉' },
    { href: '/notes', label: 'Notes', symbol: '▤' },
    { href: '/rules', label: 'Rules', symbol: '⌘' },
    { href: '/review', label: 'Review', symbol: '✓' },
  ];
</script>

<svelte:head><title>Kiroku · Stream notebook</title></svelte:head>
<header class="site-header">
  <a class="brand" href="/"
    ><span class="brand-mark">記</span>kiroku<span class="brand-caption"
      >stream notebook</span
    ></a
  >{#if $access.owner}<form action="/auth/logout" method="POST">
      <button>Log out</button>
    </form>
  {:else if login && !offline}<a
      class="private-chip"
      href="/auth/login"
      data-sveltekit-reload>Owner login</a
    >
  {:else}<span class="private-chip">Read-only</span>{/if}
</header>
<nav aria-label="Main navigation">
  {#each tabs.filter((tab) => tab.href !== '/review' || $access.owner) as tab}<a
      href={tab.href}
      class:active={tab.href === '/'
        ? page.url.pathname === '/' || page.url.pathname.startsWith('/session')
        : page.url.pathname.startsWith(tab.href)}
      ><span aria-hidden="true">{tab.symbol}</span>{tab.label}</a
    >{/each}
</nav>
<main>
  {#if offline}<section class="empty" role="status">
      <h1>You’re offline</h1>
      <p>Connect to read the latest transcript, Notes and alerts.</p>
      {#if !deviceOffline}<button onclick={() => retry()}
          >Retry connection</button
        >{/if}
    </section>
  {:else if page.url.pathname.startsWith('/review') && !$access.owner}<section
      class="empty"
    >
      <h1>Owner login required</h1>
      <p>The Review queue is available to the owner.</p>
    </section>
  {:else}{#key page.url.pathname + page.url.search}{@render children()}{/key}{/if}
</main>
<footer>Listen closely. Keep what matters.</footer>
