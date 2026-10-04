<script setup lang="ts">
import { accountHref, accountsPill } from '~/utils/accounts'

// A red chip beside the activity status while any coding account is down.
// Unlike the alert bar it cannot be dismissed.
const { accounts, down } = useCodingAccounts()
const label = computed(() => accountsPill(accounts.value))
const to = computed(() => down.value[0] ? accountHref(down.value[0].provider) : '/settings/harnesses')
</script>

<template>
  <NuxtLink
    v-if="label"
    :to="to"
    data-accounts-pill
    :title="label"
    class="inline-flex items-center gap-1.5 rounded-full border border-error/50 bg-error/15 px-2.5 py-0.5 text-xs font-medium text-error hover:bg-error/25"
  >
    <span class="size-1.5 rounded-full bg-error" aria-hidden="true" />{{ label }}
  </NuxtLink>
</template>
