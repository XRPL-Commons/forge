<script setup lang="ts">
const {
  detail, pending, decisionRequest, decision, canAssign, closeOpen, assigning, assignRequest,
  preselected, candidatePull, approvalOpen, sentToGitHub, approval, candidateUrl, decisionReason,
  pullUrl, askReviewer, closeReviewer, assignReviewer, closeDecision, submit,
} = useIssueDetailContext()
</script>
<template>
  <template v-if="detail">
      <LazyApprovalDialog
        v-if="approvalOpen && approval && detail"
        :reason="approval.reason"
        :verification="!!detail.verification_request"
        :url="pullUrl(approval.pr) + '/files'"
        :can-assign="canAssign"
        @opened="sentToGitHub = true"
        @close="approvalOpen = false"
        @assign="askReviewer"
      />
      <ReviewerDialog
        v-if="assigning"
        :open="assigning"
        :reviewers="detail.reviewers ?? []"
        :preselected="preselected"
        :pull="candidatePull"
        :pull-url="candidateUrl"
        :error="assignRequest.error.value"
        :pending="assignRequest.pending.value"
        @submit="assignReviewer"
        @close="closeReviewer"
      />

      <CloseIssueDialog
        v-if="closeOpen"
        :open="closeOpen"
        :issue-key="detail.key"
        :number="detail.number"
        :pull="candidatePull?.number ?? null"
        :reason="decisionReason"
        :error="decisionRequest.error.value"
        :pending="decisionRequest.pending.value"
        @submit="submit"
        @close="closeDecision"
      />
      <DecisionPanel
        v-if="decision && decision !== 'close'"
        :open="!!decision"
        :action="decision"
        :detail="detail"
        :error="decisionRequest.error.value"
        :pending="decisionRequest.pending.value"
        :reason="decisionReason"
        @submit="submit"
        @close="closeDecision"
      />

  </template>
</template>
