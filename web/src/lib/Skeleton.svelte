<script lang="ts">
  let { label }: { label: string } = $props();
</script>

<div class="card skeleton" role="status" aria-label={label}>
  <span class="skeleton-line short"></span>
  <span class="skeleton-line"></span>
  <span class="skeleton-line"></span>
  <span class="sr-only">{label}</span>
</div>
