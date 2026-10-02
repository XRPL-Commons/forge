<script setup lang="ts">
// One quiet, collapsible section under the issue's next step: a title row
// that opens it, nothing else until it is opened.
withDefaults(defineProps<{ title: string; unmountOnHide?: boolean }>(), { unmountOnHide: true })
const open = defineModel<boolean>('open', { default: false })
</script>

<template>
  <section class="border-t border-line py-1">
    <UCollapsible v-model:open="open" :unmount-on-hide="unmountOnHide">
      <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between px-1 text-muted">
        {{ title }}
        <UIcon name="i-lucide-chevron-down" class="size-4 transition-transform" :class="open ? 'rotate-180' : ''" />
      </UButton>
      <template #content>
        <div class="px-1 pb-2">
          <slot />
        </div>
      </template>
    </UCollapsible>
  </section>
</template>
