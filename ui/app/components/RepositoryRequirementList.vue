<script setup lang="ts">
import type { ProtectionRequirement } from '~/utils/repositories'
defineProps<{ items: ProtectionRequirement[] }>()
</script>
<template>
  <ul class="flex flex-col gap-2">
    <li v-for="requirement in items" :key="requirement.id" class="flex gap-2 text-xs">
      <span aria-hidden="true" :class="requirement.passed ? 'text-mint-200' : 'text-amber-200'">{{ requirement.passed ? '✓' : '○' }}</span>
      <div>
        <strong class="text-sm font-medium text-white">{{ requirement.label }}</strong>
        <p class="text-muted">{{ requirement.detail }}</p>
      </div>
    </li>
  </ul>
</template>
