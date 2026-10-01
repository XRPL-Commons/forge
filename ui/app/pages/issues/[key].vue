<script setup lang="ts">
// The issue's own route: the one shared link, and the only detail view.
// Everything it shows — including the worker output for `?job=<id>` — lives
// in IssueDetail, keyed by the issue so that opening another one starts from
// a clean panel instead of inheriting the previous issue's state.
const route = useRoute()
const issueKey = computed(() => String(route.params.key ?? ''))
const number = computed(() => issueKey.value.split(':')[1] ?? issueKey.value)

useHead({ title: () => `Issue #${number.value}` })
</script>

<template>
  <div>
    <UButton id="issue-back" to="/issues" size="xs" color="neutral" variant="link" icon="i-lucide-arrow-left" class="mb-3 px-0">All issues</UButton>
    <IssueDetail :key="issueKey" :issue-key="issueKey" />
  </div>
</template>
