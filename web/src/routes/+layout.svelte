<script lang="ts">
  import { onNavigate } from '$app/navigation';
  import { reducedMotion, fadeOptions } from '#lib/motion.ts';
  import { fade } from 'svelte/transition';
  import { onMount } from 'svelte';
  import { serverAvailable } from '#lib/connectivity.ts';
  import {
    access,
    loginAvailable,
    identityError,
    monitorAccess,
  } from '#lib/access.ts';
  import { page } from '$app/state';
  import './style.css';
  let { children } = $props();
  const offline = $derived(!$serverAvailable);
  let retry = () => {};
  onMount(() => {
    const monitor = monitorAccess();
    retry = monitor.retry;
    return monitor.stop;
  });
  onNavigate((navigation) => {
    if (reducedMotion() || !document.startViewTransition) return;
    return new Promise<void>((resolve) => {
      const transition = document.startViewTransition(async () => {
        resolve();
        await navigation.complete;
      });
      // A skipped transition must never hold navigation or lifecycle cleanup.
      void transition.updateCallbackDone.catch(() => resolve());
    });
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
  {:else if $loginAvailable && !offline}<a
      class="private-chip"
      href="/auth/login"
      data-sveltekit-reload>Owner login</a
    >
  {:else}<span class="private-chip"
      >{$access.owner === null ? 'Checking owner login…' : 'Read-only'}</span
    >{/if}
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
  {#if $identityError}<p role="status">{$identityError}</p>{/if}
  {#if offline}<section class="empty" role="status" in:fade={fadeOptions()}>
      <h1>You’re offline</h1>
      <p>Connect to read the latest transcript, Notes and alerts.</p>
      <button onclick={() => retry()}>Retry connection</button>
    </section>
  {:else if page.url.pathname.startsWith('/review') && !$access.owner}<section
      class="empty"
    >
      <h1>
        {$access.owner === null
          ? 'Checking owner login…'
          : 'Owner login required'}
      </h1>
      <p>The Review queue is available to the owner.</p>
    </section>
  {:else}{#key page.url.pathname + page.url.search}{@render children()}{/key}{/if}
</main>
<footer>
  <span>Listen closely. Keep what matters.</span>
  <a href="https://orulon.com">More esports projects on Orulon ↗</a>
</footer>
