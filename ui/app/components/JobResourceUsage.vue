<script setup lang="ts">
import type { ResourceUsage } from '~/utils/factory'

defineProps<{ usage: ResourceUsage }>()
function seconds(value: number) { return `${value.toFixed(1)} s` }
function memory(value: number) { return `${(value / 1024 / 1024).toFixed(1)} MiB` }
function timestamp(value: number) { return new Date(value * 1000).toISOString() }
</script>
<template>
  <div class="text-xs text-muted" aria-label="Measured execution resources">
    <p v-if="usage.wall.seconds !== null">Wall duration {{ seconds(usage.wall.seconds) }} · container start to finish</p>
    <p v-else>Wall duration unavailable</p>
    <template v-if="usage.sample">
      <p>
        CPU <span v-if="usage.sample.cpu_seconds !== null">{{ seconds(usage.sample.cpu_seconds) }} cumulative</span><span v-else>unavailable</span>
        · Memory <span v-if="usage.sample.memory_bytes !== null">{{ memory(usage.sample.memory_bytes) }} including cache</span><span v-else>unavailable</span>
      </p>
      <p>Engine sample <time :datetime="timestamp(usage.sample.sampled_at)">{{ timestamp(usage.sample.sampled_at) }}</time> · sampled values, not final totals</p>
      <p v-if="usage.collection.status === 'unavailable'">Latest resource sample unavailable; showing the retained sample.</p>
    </template>
    <p v-else>CPU and memory unavailable · no engine sample recorded</p>
  </div>
</template>
