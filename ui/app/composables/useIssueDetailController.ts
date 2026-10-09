import { decisionFeedback, receiptAction, type DecisionReceipt } from '~/utils/decision-feedback'
import { ForgeApiError } from '~/composables/useForgeApi'
import { deploymentLine, type FactoryView, type JobView } from '~/utils/factory'
import { issueTab } from '~/utils/discussion'
import { statusBadge } from '~/utils/tones'
import { issuePipeline, type PipelineView } from '~/utils/pipeline'
import { workbenchLook } from '~/utils/workbench'
import { panelQuery } from '~/utils/panel'
import {
  ACTION_LABELS, approvalLines, attentionNote, blockingIssues, statusLabel, checkedText, eventLabel, heldByLines, issuePath, lastAction, pendingAction, preselectedReviewers,
  pullLabel, reviewPull, reviewRequestText, reviewerReason, summaryBlock,
  type IssueAction, type IssueDetail as IssueDetailView, type IssueDetailResponse,
  type IssueEnvironmentSelection,
} from '~/utils/issues'


export function useIssueDetailController(props: { issueKey: string; fill?: 'container' | 'viewport' }) {
  const UNMANAGED = 'This issue is not in a managed repository. Refresh the issue list.'
  const PRIMARY_ACTIONS: IssueAction[] = ['assign_reviewer', 'retry', 'recheck', 'resume', 'queue']

  const api = useForgeApi()
  const route = useRoute()

  interface Loaded { detail: IssueDetailResponse | null; fence: string | null; factory: Pick<FactoryView, 'jobs' | 'issues' | 'repositories' | 'deployments'> | null }

  const submitting = ref(false)
  const environmentOpen = ref(false)
  const environmentRequest = useSubmission('Could not save the environment.')
  const verificationRequest = useSubmission('Could not confirm verification.')
  const verificationStatement = ref('')
  const verificationIdentity = ref<{ gate: string; revision: string; statement: string; id: string } | null>(null)

  const requestedJob = computed<string | null>(() => {
    const asked = route.query.job
    const job = Array.isArray(asked) ? asked[0] : asked
    return typeof job === 'string' && job ? job : null
  })

  const store = useFactoryStore()
  const factory = useFactoryBoard()
  const poll = useResource<Omit<Loaded, 'factory'>>(() => `/api/issues?issue=${encodeURIComponent(props.issueKey)}`, async (path) => {
    try { return { detail: await api.get<IssueDetailResponse>(path, { timeout: 30000 }), fence: null } }
    catch (error) {
      if (error instanceof ForgeApiError && error.status === 200) return { detail: null, fence: error.message }
      if (error instanceof ForgeApiError && error.status === 404) return { detail: null, fence: UNMANAGED }
      throw error
    }
  }, { interval: false, allowed: () => !submitting.value && !environmentOpen.value,
    onRead: value => { if (value.detail) store.rememberDetail(value.detail) },
  })
  const initial = import.meta.server ? poll.refresh(5000) : Promise.resolve()
  const mounted = ref(false)
  // A recovery run may outlive its issue inventory row. Only that explicit job link
  // can load the old scoped run projection; normal issue detail already contains jobs.
  const recovery = useFactory(() => '/api/factory?issue=' + encodeURIComponent(props.issueKey), {
    server: false, allowed: () => poll.data.value?.fence === UNMANAGED && !!requestedJob.value,
  })
  onMounted(() => { mounted.value = true; poll.start(); factory.start(); recovery.start() })
  const loaded = computed<Loaded | null>(() => {
    const value = poll.data.value
    if (!value) return null
    const board = factory.data.value?.ok ? factory.data.value : null
    const output = value.detail
    return { ...value, factory: output ? { repositories: board?.repositories, deployments: output.deployments,
      jobs: output.jobs, issues: output.factory_issue ? [output.factory_issue] : [] }
      : recovery.data.value?.ok ? recovery.data.value : null }
  })
  const projection = computed(() => store.issues.value.get(props.issueKey))
  const detail = computed<IssueDetailView | null>(() => {
    const issue = loaded.value?.detail?.issue
    if (!issue) return null
    const latest = projection.value
    // Feed summaries can update the visible status immediately, while changed bodies
    // wait for their lazy detail read. Open decisions retain their original revision.
    if (!submitting.value && !environmentOpen.value && latest && latest.content_hash === issue.content_hash && latest.revision !== issue.revision) return { ...issue, ...latest }
    return issue
  })
  watch(() => detail.value?.verification_request?.gate_id, (gate, previous) => {
    if (previous && gate !== previous) {
      verificationStatement.value = ''
      verificationIdentity.value = null
      verificationRequest.error.value = gate ? 'The verification request changed. Check the current candidate before confirming.' : null
    }
  })
  const actions = computed(() => loaded.value?.detail?.actions ?? [])
  const fence = computed(() => loaded.value?.fence ?? null)

  const problem = computed(() => fence.value ?? (detail.value ? null : poll.error.value))

  const pending = computed(() => (detail.value ? pendingAction(actions.value, detail.value.key) : null))
  const busy = computed(() => submitting.value || !!pending.value)

  const rendered = useAsyncData(`md-${props.issueKey}`, async () => {
    await initial
    const issue = detail.value
    if (!issue?.body) return ''
    const rendered = await $fetch<{ html: string }>('/_ui/markdown', { method: 'POST', body: { text: issue.body, repo: issue.repo } })
    return rendered.html
  }, { watch: [() => detail.value?.body], lazy: true })
  const html = rendered.data
  const ready = Promise.all([initial, rendered])

  const submitted = ref<DecisionReceipt | null>(null)
  const workerReceipt = ref<{ id: string; before: string } | null>(null)
  const decisionRequest = useSubmission()
  const decision = ref<IssueAction | null>(null)

  const observedDecision = computed(() => workerReceipt.value ? null : receiptAction(actions.value, props.issueKey, submitted.value))
  const message = computed(() => decisionFeedback(observedDecision.value, submitted.value, poll.stale.value))
  watch([observedDecision, submitted], ([action]) => {
    if (submitted.value && action && action.status !== 'pending') submitted.value = null
  })
  watch(() => props.issueKey, () => { submitted.value = null; workerReceipt.value = null })

  function previousDecision(): string | null {
    return lastAction(actions.value, props.issueKey)?.id ?? null
  }
  function recordDecision(reply: { request?: string }, previous: string | null, message?: string) {
    workerReceipt.value = null
    submitted.value = { request: reply.request ?? null, previous, message }
  }


  const summary = computed(() => summaryBlock(detail.value))
  const pipeline = computed(() => (detail.value ? issuePipeline(detail.value) : null))
  const look = computed(() => (detail.value ? workbenchLook(detail.value) : null))
  const badge = computed(() => statusBadge(detail.value?.status ?? 'queued'))
  const blocking = computed(() => (detail.value ? blockingIssues(detail.value) : []))
  function siblingLink(number: number) {
    const key = `${detail.value?.repo_id}:${number}`
    return route.path === '/' ? { path: '/', query: panelQuery(route.query, { key }) } : issuePath(key)
  }
  const NOTE_TONE: Record<PipelineView['state'], string> = {
    done: 'text-mint-200', active: 'text-sky-200', waiting: 'text-sky-200', failed: 'text-rose-200',
    blocked: 'text-amber-200', upcoming: 'text-muted', closed: 'text-muted',
  }
  const descriptionOpen = ref(false)
  const environmentSectionOpen = ref(false)
  const conversation = ref<{ focus: () => boolean } | null>(null)
  const plainOpen = ref(false)
  const PLAIN_CLAMP = 220
  const plainLong = computed(() => (summary.value?.plain ?? '').length > PLAIN_CLAMP)
  const holds = computed(() => heldByLines(detail.value?.held_by))
  const hasFindings = computed(() => !!summary.value?.details || reviewRows(detail.value?.reviews).length > 0)

  const issueUrl = computed(() => (detail.value ? `https://github.com/${detail.value.repo}/issues/${detail.value.number}` : ''))
  function pullUrl(number: number) { return `https://github.com/${detail.value?.repo}/pull/${number}` }

  const canAssign = computed(() => !!detail.value?.actions.includes('assign_reviewer'))
  const decisions = computed(() => (detail.value?.actions ?? []).filter(action => action !== 'assign_reviewer'))
  const setupNeeded = computed(() => !!(detail.value?.setup_needed || detail.value?.merge_blocker))
  const primary = computed<'approve' | 'setup' | IssueAction | null>(() => {
    if (approval.value) return 'approve'
    const action = PRIMARY_ACTIONS.find(item => (detail.value?.actions ?? []).includes(item as never))
    if (action) return action
    return setupNeeded.value ? 'setup' : null
  })
  const secondary = computed(() => decisions.value.filter(action => action !== primary.value))
  const closeOpen = computed(() => decision.value === 'close')
  const assigning = ref(false)
  const assignRequest = useSubmission()
  const preselected = computed(() => preselectedReviewers(detail.value))
  const candidatePull = computed(() => reviewPull(detail.value?.pull_requests))
  const approvalOpen = ref(false)
  const sentToGitHub = ref(false)
  const approval = computed(() => detail.value?.approval ?? null)
  watch([approval, () => route.query.approve], ([value, asked]) => {
    if (value && asked === '1') {
      approvalOpen.value = true
      void navigateTo({ path: route.path, query: { ...route.query, approve: undefined } }, { replace: true })
    }
    if (!value) approvalOpen.value = false
  }, { immediate: true })
  const candidateUrl = computed(() => (candidatePull.value ? pullUrl(candidatePull.value.number) : issueUrl.value))

  function askReviewer() {
    approvalOpen.value = false
    assignRequest.reset()
    assigning.value = true
    submitting.value = true
  }
  function closeReviewer() {
    if (assignRequest.pending.value) return
    assigning.value = false
    assignRequest.reset()
    submitting.value = false
  }

  const now = ref(0)
  let ticker: ReturnType<typeof setInterval> | undefined
  onMounted(() => {
    now.value = Math.floor(Date.now() / 1000)
    ticker = setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 5000)
  })
  onBeforeUnmount(() => clearInterval(ticker))
  const reviewRequested = computed(() => reviewRequestText(detail.value?.reviewers_requested, now.value))

  function shortTime(at: number): string {
    const date = new Date(at * 1000)
    const today = new Date().toDateString() === date.toDateString()
    return date.toLocaleString([], today ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }
  const approvals = computed(() => approvalLines(detail.value?.approvals, mounted.value ? shortTime : undefined))
  const checked = computed(() => checkedText(detail.value?.last_checked, now.value))

  const CHECK_POLL_MS = 4000
  const CHECK_GIVE_UP_S = 90
  const checkingSince = ref<number | null>(null)
  const checkNote = ref('')
  const checkRequest = useSubmission('Forge did not accept the request.')
  let checkTimer: ReturnType<typeof setInterval> | undefined
  function stopChecking(note = '') {
    clearInterval(checkTimer)
    checkingSince.value = null
    checkNote.value = note
  }
  async function checkNow() {
    const issue = detail.value
    if (!issue || checkingSince.value || checkRequest.pending.value) return
    checkNote.value = ''
    const since = Math.floor(Date.now() / 1000)
    if (!await checkRequest.run(() => api.act('/api/issues/refresh', { key: issue.key }))) {
      checkNote.value = checkRequest.error.value ?? ''
      return
    }
    checkingSince.value = since
    clearInterval(checkTimer)
    checkTimer = setInterval(() => {
      if (document.hidden) return
      if (Date.now() / 1000 - since > CHECK_GIVE_UP_S) stopChecking('Still checking. Forge reads GitHub every cycle, so this page will update on its own.')
      else void poll.refresh()
    }, CHECK_POLL_MS)
    void poll.refresh()
  }
  watch(() => detail.value?.last_checked, (value) => {
    if (checkingSince.value && value && value > checkingSince.value) stopChecking()
  })
  watch(() => props.issueKey, () => stopChecking())
  onBeforeUnmount(() => clearInterval(checkTimer))

  const deployment = computed(() => {
    const factory = loaded.value?.factory ?? null
    return deploymentLine((factory?.issues ?? []).find(issue => issue.key === props.issueKey), factory, now.value)
  })

  async function assignReviewer(logins: string[]) {
    const issue = detail.value
    const reason = reviewerReason(logins)
    if (!issue || !reason) return
    const done = await assignRequest.run(async () => {
      const previous = previousDecision()
      const reply = await api.act<{ request?: string }>('/api/issues/action', {
        key: issue.key, action: 'assign_reviewer', revision: issue.revision, reason,
      })
      recordDecision(reply, previous)
    })
    if (!done) return
    closeReviewer()
    await poll.refresh()
  }

  function localTime(at: number): string {
    const iso = new Date(at * 1000).toISOString()
    return mounted.value ? new Date(at * 1000).toLocaleString() : iso
  }
  function isoTime(at: number): string { return new Date(at * 1000).toISOString() }

  const jobs = computed<JobView[]>(() => {
    const live = store.normalizeJobs((loaded.value?.factory?.jobs ?? []).filter(job => job.issue_key === props.issueKey))
    const row = (loaded.value?.factory?.issues ?? []).find(issue => issue.key === props.issueKey)
    const past = (row?.attempts ?? [])
      .filter(job => !live.some(running => running.id === job.id))
      .map(job => ({ ...job, issue_key: props.issueKey }) as JobView)
    return [...live, ...past]
      .sort((left, right) => (right.started_at ?? right.created_at ?? 0) - (left.started_at ?? left.created_at ?? 0))
  })


  const selectedJob = computed<string | null>(() => requestedJob.value ?? detail.value?.active ?? jobs.value[0]?.id ?? null)

  const showOutput = computed(() => !!detail.value || !!requestedJob.value)

  const internalJob = computed<JobView | null>(() => {
    const id = requestedJob.value
    if (!id) return null
    const factory = loaded.value?.factory
    const job = (factory?.jobs ?? []).find(row => row.id === id) ?? null
    if (!job) return null
    return (factory?.issues ?? []).some(issue => issue.active === id) ? null : job
  })

  function workerState(id: string): string {
    const job = jobs.value.find(job => job.id === id)
    return JSON.stringify(job ? [job.phase, job.outcome, job.cleanup] : null)
  }
  watch(() => workerReceipt.value && workerState(workerReceipt.value.id), value => {
    if (workerReceipt.value && value !== workerReceipt.value.before) {
      workerReceipt.value = null
      submitted.value = null
    }
  })

  const RESET = {
    title: 'Reset runner?',
    text: 'This terminates the old execution and discards its disposable workspace. Capacity is released only after teardown is verified.',
    confirmLabel: 'Reset runner',
  }
  const resetting = ref(false)
  const resetRequest = useSubmission()

  function askReset() {
    resetRequest.reset()
    resetting.value = true
    submitting.value = true
  }
  function closeReset() {
    if (resetRequest.pending.value) return
    resetting.value = false
    submitting.value = false
  }
  async function confirmReset(reason: string) {
    const job = internalJob.value
    if (!job) return
    const done = await resetRequest.run(async () => {
      const before = workerState(job.id)
      await api.act('/api/factory/action', { action: 'reset', target: job.id, reason })
      submitted.value = { request: null, previous: previousDecision() }
      workerReceipt.value = { id: job.id, before }
    })
    if (!done) return
    closeReset()
    await poll.refresh()
  }

  const outputOpen = ref(false)
  const activityOpen = ref(false)

  const decisionReason = ref('')
  function ask(action: IssueAction, reason = '') {
    decisionRequest.reset()
    decisionReason.value = reason
    decision.value = action
    submitting.value = true
  }

  async function confirmVerification() {
    const issue = detail.value
    const gate = issue?.verification_request?.gate_id
    const statement = verificationStatement.value.trim()
    if (!issue || !gate || !statement || busy.value) return
    if (verificationIdentity.value?.gate !== gate || verificationIdentity.value?.revision !== issue.revision || verificationIdentity.value?.statement !== statement) {
      verificationIdentity.value = { gate, revision: issue.revision, statement, id: crypto.randomUUID() }
    }
    const requestId = verificationIdentity.value.id
    submitting.value = true
    const confirmed = await verificationRequest.run(() => api.act('/api/issues/verify', {
      key: issue.key, revision: issue.revision, gate_id: gate, statement, request_id: requestId,
    }))
    submitting.value = false
    if (confirmed) verificationStatement.value = ''
    await poll.refresh()
    if (confirmed) await factory.refresh()
  }

  async function saveEnvironment(environment: IssueEnvironmentSelection) {
    const issue = detail.value
    if (!issue || busy.value) return
    submitting.value = true
    await environmentRequest.run(async () => {
      const previous = previousDecision()
      const reply = await api.act<{ request?: string }>('/api/issues/environment', { key: issue.key, revision: issue.revision, environment })
      recordDecision(reply, previous, 'Environment preference submitted. Waiting for the supervisor to apply it.')
      environmentOpen.value = false
    })
    submitting.value = false
    if (!environmentOpen.value) await poll.refresh()
  }
  function closeDecision() {
    if (decisionRequest.pending.value) return
    decision.value = null
    decisionRequest.reset()
    submitting.value = false
  }

  async function submit({ action, reason }: { action: IssueAction; reason: string }) {
    const issue = detail.value
    if (!issue) return
    let applied = false
    const done = await decisionRequest.run(async () => {
      const previous = previousDecision()
      if (action === 'stop') {
        const before = workerState(issue.active!)
        await api.act('/api/factory/action', { action: 'stop', target: issue.active, reason })
        submitted.value = { request: null, previous }
        workerReceipt.value = { id: issue.active!, before }
      } else {
        const reply = await api.act<IssueDetailResponse & { request?: string }>('/api/issues/action', { key: issue.key, action, revision: issue.revision, reason })
        recordDecision(reply, previous)
        if (reply.issue) {
          poll.replace({ detail: reply, fence: null })
          submitted.value = null
          applied = true
        }
      }
    })
    if (!done) return
    decision.value = null
    submitting.value = false
    if (!applied) await poll.refresh()
  }
  const { root, rootClass, filling, fillHeight, tab, TABS, chat, chatAttention } = useIssueDetailTabs(props, detail, conversation)
  return {
    environmentOpen, environmentRequest, verificationRequest, verificationStatement, poll, detail,
    actions, problem, pending, busy, rendered, html, ready, decisionRequest, decision, message,
    summary, pipeline, look, badge, blocking, NOTE_TONE, descriptionOpen, environmentSectionOpen,
    conversation, plainOpen, plainLong, holds, hasFindings, issueUrl, canAssign, decisions,
    setupNeeded, primary, secondary, closeOpen, assigning, assignRequest, preselected, candidatePull,
    approvalOpen, sentToGitHub, approval, candidateUrl, now, reviewRequested, approvals, checked,
    checkingSince, checkNote, checkRequest, deployment, jobs, selectedJob, showOutput, internalJob,
    RESET, resetting, resetRequest, outputOpen, activityOpen, decisionReason, siblingLink, pullUrl,
    askReviewer, closeReviewer, checkNow, assignReviewer, localTime, isoTime, askReset, closeReset,
    confirmReset, ask, confirmVerification, saveEnvironment, closeDecision, submit, root, rootClass,
    filling, fillHeight, tab, TABS, chat, chatAttention, ACTION_LABELS, attentionNote, statusLabel,
    eventLabel, pullLabel, issueTab,
  }
}

export type IssueDetailController = ReturnType<typeof useIssueDetailController>
