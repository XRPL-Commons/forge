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
  <IssueDetail :key="issueKey" :issue-key="issueKey" />
</template>
