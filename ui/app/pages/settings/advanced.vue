<script setup lang="ts">
// /settings/advanced is General under another URL (contracts §3.4: both
// paths render the same page). SettingsTabs already highlights General for
// this path; re-exporting the same component keeps the two routes in sync
// with zero duplicated logic.
import General from './index.vue'
</script>

<template>
  <General />
</template>
