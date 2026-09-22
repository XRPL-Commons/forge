<script setup lang="ts">
import type { Harness } from '~/utils/onboarding'

// Sign-in and verification commands for one harness (contracts §3.7).
// Both commands are run by the operator in a terminal on the Forge VM; this
// dialog only shows them and helps copy them.
const props = defineProps<{ harness: Harness | null; open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const COPIED = 'Copied'
// The UI is served over plain HTTP on the tailnet, so `navigator.clipboard`
// is usually undefined: selecting the command is the normal path, not an
// error path.
const FALLBACK = 'Select and copy the command above'

const loginLabel = ref('Copy sign-in command')
const checkLabel = ref('Copy verification command')
const tokenLabel = ref('Copy token sign-in command')
const loginCommand = useTemplateRef<HTMLElement>('loginCommand')
const checkCommand = useTemplateRef<HTMLElement>('checkCommand')
const tokenCommand = useTemplateRef<HTMLElement>('tokenCommand')

watch(() => [props.open, props.harness?.id], () => {
  loginLabel.value = 'Copy sign-in command'
  checkLabel.value = 'Copy verification command'
  tokenLabel.value = 'Copy token sign-in command'
})

function selectCommand(element: HTMLElement | null) {
  if (!element) return
  const range = document.createRange()
  range.selectNodeContents(element)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

// Keyed rather than handed the ref: a ref passed through the template is
// already unwrapped, so the label has to be resolved here.
async function copy(kind: 'login' | 'check' | 'token') {
  const command = kind === 'login' ? props.harness?.login_command
    : kind === 'token' ? props.harness?.token_login_command : props.harness?.check_command
  const element = kind === 'login' ? loginCommand.value : kind === 'token' ? tokenCommand.value : checkCommand.value
  const label = kind === 'login' ? loginLabel : kind === 'token' ? tokenLabel : checkLabel
  try {
    await navigator.clipboard.writeText(command ?? '')
    label.value = COPIED
  } catch {
    label.value = FALLBACK
    selectCommand(element)
  }
}
</script>

<template>
  <UModal
    :open="open"
    :title="harness ? (harness.connected ? 'Verify ' : 'Connect ') + harness.name : ''"
    :close="false"
    @update:open="value => !value && emit('close')"
  >
    <template #body>
      <div v-if="harness" class="space-y-3 text-sm">
        <template v-if="!harness.connected">
          <p class="text-muted">Run this command in a terminal on the Forge VM, as the Forge service account. Follow the harness’s sign-in instructions.</p>
          <pre ref="loginCommand" class="rounded-lg border border-line bg-bg p-3 text-[13px] whitespace-pre-wrap break-words">{{ harness.login_command }}</pre>
          <UButton color="neutral" variant="outline" @click="copy('login')">{{ loginLabel }}</UButton>
          <template v-if="harness.token_login_command">
            <p class="text-muted">
              Or share one long-lived token so several workers can use this account at once:
              run <code class="text-white">{{ harness.token_command }}</code> on your own machine, then paste the token here:
            </p>
            <pre ref="tokenCommand" class="rounded-lg border border-line bg-bg p-3 text-[13px] whitespace-pre-wrap break-words">{{ harness.token_login_command }}</pre>
            <UButton color="neutral" variant="outline" @click="copy('token')">{{ tokenLabel }}</UButton>
          </template>
        </template>
        <p class="text-muted">
          {{ harness.connected
            ? 'Run this command in a terminal on the Forge VM, as the Forge service account, to verify the connection:'
            : 'After signing in, verify the connection:' }}
        </p>
        <pre ref="checkCommand" class="rounded-lg border border-line bg-bg p-3 text-[13px] whitespace-pre-wrap break-words">{{ harness.check_command }}</pre>
        <UButton color="neutral" variant="outline" @click="copy('check')">{{ checkLabel }}</UButton>
        <p class="text-muted">Account status updates automatically after verification.</p>
      </div>
    </template>
    <template #footer>
      <div class="flex justify-end">
        <UButton color="neutral" variant="ghost" @click="emit('close')">Close</UButton>
      </div>
    </template>
  </UModal>
</template>
