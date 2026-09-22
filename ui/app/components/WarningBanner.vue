<script setup lang="ts">
import type { Warning } from '~/utils/factory'

// `title` is optional: the Activity page passes the operator status title when
// a banner absorbs the status line (they say the same thing), so the message
// is rendered exactly once.
defineProps<{ warning: Warning; title?: string }>()
</script>

<template>
  <UAlert
    role="status"
    color="warning"
    variant="subtle"
    :title="title"
    :description="warning.text"
    :actions="warning.to ? [{ label: warning.label, to: warning.to, color: 'neutral', variant: 'outline' }] : []"
  />
</template>
