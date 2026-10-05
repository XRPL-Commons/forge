<script setup lang="ts">
// The one footer layout for modals and inline confirm forms. Buttons sit at
// the right edge in the order [secondary…] [primary], so the primary action
// is always bottom-right. A wizard may put Back in `start`, which stays at
// the far left. Below 480px every button is full width and the stack is
// reversed, so the primary is on top and Back is at the bottom. DOM order,
// and therefore keyboard order, is unchanged: start, secondary, primary.
</script>

<template>
  <div data-dialog-footer class="flex w-full flex-col-reverse gap-2 min-[480px]:flex-row min-[480px]:items-center">
    <div v-if="$slots.start" class="flex flex-col *:w-full *:justify-center min-[480px]:flex-row min-[480px]:*:w-auto">
      <slot name="start" />
    </div>
    <div class="flex flex-col-reverse gap-2 *:w-full *:justify-center min-[480px]:ml-auto min-[480px]:flex-row min-[480px]:items-center min-[480px]:*:w-auto">
      <slot name="secondary" />
      <slot name="primary" />
    </div>
  </div>
</template>
