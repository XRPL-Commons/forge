<script setup lang="ts">
// Original Forge artwork from forge/illustrations.py before the Nuxt migration
// (0573d34^). Keep both surfaces on this shared component to avoid another loss.
defineProps<{ role: string; github?: boolean }>()
</script>

<template>
  <svg v-if="['build', 'review', 'audit', 'controller'].includes(role)"
    class="role-illustration shrink-0" :data-role="role" viewBox="0 0 80 80"
    aria-hidden="true" focusable="false" fill="none"
  >
    <g v-if="role === 'build'">
      <rect x="5" y="5" width="65" height="65" rx="17" fill="currentColor" opacity=".07" transform="rotate(-5 38 38)"/>
      <rect x="8" y="5" width="63" height="63" rx="16" fill="none" stroke="currentColor" stroke-opacity=".22"/>
      <g fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
      <rect x="19" y="37" width="17" height="17" rx="3" fill="currentColor" fill-opacity=".25"/>
      <rect x="39" y="37" width="17" height="17" rx="3"/>
      <rect x="29" y="16" width="17" height="17" rx="3" transform="rotate(-9 37 24)" fill="currentColor" fill-opacity=".4"/>
      </g>
      </g>
    <g v-if="role === 'review'">
      <rect x="5" y="5" width="65" height="65" rx="17" fill="currentColor" opacity=".07" transform="rotate(-5 38 38)"/>
      <rect x="8" y="5" width="63" height="63" rx="16" fill="none" stroke="currentColor" stroke-opacity=".22"/>
      <g fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
      <rect x="21" y="17" width="30" height="38" rx="4" transform="rotate(-7 36 36)" fill="currentColor" fill-opacity=".13"/>
      <path d="M29 27h14m-14 7h8"/>
      <circle cx="46" cy="44" r="10" fill="#20232a"/>
      <path d="m53 51 7 7m-18-14 3 3 5-6"/>
      </g>
      </g>
    <g v-if="role === 'audit'">
      <rect x="5" y="5" width="65" height="65" rx="17" fill="currentColor" opacity=".07" transform="rotate(-5 38 38)"/>
      <rect x="8" y="5" width="63" height="63" rx="16" fill="none" stroke="currentColor" stroke-opacity=".22"/>
      <g fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
      <path d="M38 16c7 5 12 6 18 7v14c0 10-9 17-18 21-9-4-18-11-18-21V23c6-1 11-2 18-7Z" fill="currentColor" fill-opacity=".17"/>
      <path d="m29 36 6 6 12-13m13-17v8m-4-4h8"/>
      </g>
      </g>
    <g v-if="role === 'controller'">
      <rect x="5" y="5" width="65" height="65" rx="17" fill="currentColor" opacity=".07" transform="rotate(-5 38 38)"/>
      <rect x="8" y="5" width="63" height="63" rx="16" fill="none" stroke="currentColor" stroke-opacity=".22"/>
      <g fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
      <path d="m36 36-13-12m13 12 15-14M36 36l16 15M36 36 21 52"/>
      <rect x="15" y="16" width="13" height="13" rx="4" transform="rotate(-10 21 22)" fill="#302a21"/>
      <circle cx="54" cy="20" r="7" fill="currentColor" fill-opacity=".3"/>
      <rect x="47" y="46" width="13" height="13" rx="4" transform="rotate(9 53 52)" fill="#302a21"/>
      <circle cx="18" cy="55" r="6" fill="currentColor" fill-opacity=".3"/>
      <circle cx="36" cy="36" r="9" fill="currentColor"/>
      <path d="m33 36 2 2 4-4" stroke="#202328"/>
      </g>
      </g>
    <g v-if="github" data-github-badge>
      <circle cx="64" cy="64" r="15" fill="#252b30" stroke="#191c1f" stroke-width="4"/>
      <svg x="54" y="54" width="20" height="20" viewBox="0 0 24 24" fill="#edf0f2">
      <path d="M12 .297C5.37.297 0 5.67 0 12.297c0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.043-1.61-4.043-1.61-.546-1.387-1.333-1.756-1.333-1.756-1.09-.745.083-.729.083-.729 1.205.084 1.838 1.237 1.838 1.237 1.07 1.835 2.809 1.305 3.495.998.108-.776.418-1.305.762-1.605-2.665-.305-5.466-1.334-5.466-5.93 0-1.31.469-2.381 1.236-3.221-.124-.303-.536-1.524.117-3.176 0 0 1.008-.322 3.301 1.23a11.52 11.52 0 0 1 3.003-.404c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.655 1.652.243 2.873.119 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.805 5.622-5.479 5.921.43.372.823 1.102.823 2.222 0 1.606-.015 2.898-.015 3.293 0 .322.216.694.825.576C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/>
      </svg>
      </g>
  </svg>
</template>

<style scoped>
.role-illustration[data-role="build"] { color: var(--color-role-build); }
.role-illustration[data-role="review"] { color: var(--color-role-review); }
.role-illustration[data-role="audit"] { color: var(--color-role-audit); }
.role-illustration[data-role="controller"] { color: var(--color-role-controller); }
</style>
