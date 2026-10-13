<script lang="ts">
  import { page } from '$app/state';
  import './style.css';
  let { children } = $props();
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
  ><span class="private-chip">Personal workspace</span>
</header>
<nav aria-label="Main navigation">
  {#each tabs as tab}<a
      href={tab.href}
      class:active={tab.href === '/'
        ? page.url.pathname === '/' || page.url.pathname.startsWith('/session')
        : page.url.pathname.startsWith(tab.href)}
      ><span aria-hidden="true">{tab.symbol}</span>{tab.label}</a
    >{/each}
</nav>
<main>
  {#key page.url.pathname + page.url.search}{@render children()}{/key}
</main>
<footer>Listen closely. Keep what matters.</footer>
