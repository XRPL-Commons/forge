<script setup lang="ts">
import { HELP_TOPICS, helpTopic, helpTopicHref } from '#shared/help-topics'
import type { HelpDoc } from '../../server/utils/help'

const route = useRoute()
const topicId = computed(() => typeof route.query.topic === 'string' ? route.query.topic : route.query.topic ? 'unknown' : 'setup')
const topic = computed(() => helpTopic(topicId.value))
useHead({ title: () => topic.value?.title ?? 'Help' })
const { data, error, status, refresh } = await useAsyncData(
  () => `help:${topicId.value}`,
  () => $fetch<HelpDoc>('/_ui/help', { query: { topic: topicId.value } }),
)
const toc = computed(() => data.value?.toc ?? [])
</script>

<template>
  <div class="grid min-w-0 gap-8 lg:grid-cols-[220px_minmax(0,760px)] lg:gap-12">
    <aside class="min-w-0">
      <div class="lg:sticky lg:top-24 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto">
        <nav aria-label="Help guides" class="mb-6 flex flex-wrap gap-1 lg:flex-col">
          <p class="mb-1 w-full text-xs font-semibold uppercase tracking-wide text-muted">Guides</p>
          <NuxtLink
            v-for="guide in HELP_TOPICS" :key="guide.id" :to="helpTopicHref(guide.id)"
            :aria-current="topicId === guide.id ? 'page' : undefined"
            :title="guide.description"
            class="flex min-h-11 items-center rounded-xl px-3 py-2 text-sm hover:bg-panel hover:text-white"
            :class="topicId === guide.id ? 'bg-panel text-white' : 'text-muted'"
          >{{ guide.title }}</NuxtLink>
        </nav>
        <nav v-if="toc.length && !error" aria-label="Help topics" class="flex flex-wrap gap-x-2 gap-y-1 text-sm lg:flex-col">
          <p class="mb-1 w-full text-xs font-semibold uppercase tracking-wide text-muted">On this page</p>
          <a v-for="entry in toc" :key="entry.id" :href="`${helpTopicHref(topicId)}#${entry.id}`"
            class="flex min-h-11 items-center rounded-md px-2 py-2 text-muted hover:bg-panel hover:text-white"
          >{{ entry.text }}</a>
        </nav>
      </div>
    </aside>
    <div class="min-w-0" :aria-busy="status === 'pending'">
      <p id="help-eyebrow" class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Help</p>
      <div v-if="error" role="status" class="rounded-xl border border-line bg-panel p-5">
        <h1 class="text-xl font-semibold">{{ topic ? 'Guide unavailable' : 'Help topic not found' }}</h1>
        <p class="mt-2 text-sm text-muted">{{ topic ? 'This guide could not be loaded. Try again or choose another guide.' : 'Choose a guide from the menu.' }}</p>
        <button v-if="topic" class="mt-3 min-h-11 rounded-full border border-line px-4 text-sm" @click="refresh()">Try again</button>
      </div>
      <p v-else-if="!data" role="status" class="text-muted">Loading guide…</p>
      <HelpBody v-else :key="topicId" :html="data.html" :blocks="data.blocks" />
    </div>
  </div>
</template>
