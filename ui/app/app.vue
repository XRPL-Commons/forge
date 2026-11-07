<script setup lang="ts">
const { brand } = useFactoryIdentity()
useHead(() => ({ titleTemplate: title => title ? `${title} · ${brand.value}` : brand.value }))
</script>
<template>
  <UApp>
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
