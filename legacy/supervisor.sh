#!/usr/bin/env bash
# forge-supervisor — demand-loaded reconciler + watchdog for repos in repos.d/.
# A repo's builder/reviewer sessions run only while it has actionable work
# (ready issues, wip in progress, or ready-for-review PRs). When a repo's queue
# is empty and both sessions are idle for SLEEP_AFTER_EMPTY_POLLS cycles, its
# sessions sleep; they wake on demand and resume their own conversation.
# Dormant-wake is silent; only a real crash alerts. Self-heals from crash/OOM/reboot.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR"
# Configure a stable native executable; repository shell/fnm PATH may change.
FORGE_CLAUDE_BIN="${FORGE_CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
FORGE_CODEX_BIN="${FORGE_CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
# Grok Build is optional and explicit only: never discovered from PATH.
FORGE_GROK_BIN="${FORGE_GROK_BIN:-}"
# Fleet capacity: concurrent agent sessions host-wide and per role (build/review/audit).
# Defaults reserve one slot per role. Validate capacity against your own host.
MAX_SESSIONS="${MAX_SESSIONS:-3}"
MAX_SESSIONS_PER_ROLE="${MAX_SESSIONS_PER_ROLE:-1}"
# PSI limits are tunable admission guardrails; validate them against your own host.
MAX_MEMORY_PSI_SOME="${MAX_MEMORY_PSI_SOME:-1.0}"
MAX_MEMORY_PSI_FULL="${MAX_MEMORY_PSI_FULL:-0.1}"
CAP_ARGS=(--max-sessions "$MAX_SESSIONS" --max-sessions-per-role "$MAX_SESSIONS_PER_ROLE"
          --max-memory-psi-some "$MAX_MEMORY_PSI_SOME" --max-memory-psi-full "$MAX_MEMORY_PSI_FULL")
exec 8>"$DIR/state-supervisor.lock"
flock -n 8 || exit 0
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
POLL="${SUPERVISOR_POLL:-60}"
SLEEP_AFTER_EMPTY_POLLS="${SLEEP_AFTER_EMPTY_POLLS:-2}"   # sleep after this many consecutive empty+idle polls
STATE="$DIR/state"; mkdir -p "$DIR/logs" "$STATE"
GLOG="$DIR/logs/supervisor.log"
glog(){ echo "$(date '+%F %T') $*" >>"$GLOG"; }
# Local tool failures must not hold the single fleet supervisor indefinitely.
tmux(){ timeout --kill-after=2 5 tmux "$@"; }
python3(){ timeout --kill-after=2 90 python3 "$@"; }
notify(){ "$DIR/notify.sh" "$1" >/dev/null 2>&1 || glog "notification enqueue FAILED"; }
activity(){ python3 -m forge.activity --state "$STATE" --config "$DIR/repos.d" event --session "$2" --event "$1" --reason "${3:-supervisor}" >/dev/null || glog "[$2] activity ledger write failed: $1"; }
health(){
  local reason status
  reason=$(python3 -m forge.legacy_health "$1" --state "$STATE" --session "$2"); status=$?
  [ -z "$reason" ] || printf '%s\n' "$reason"
  if [ "$status" -ne 0 ] && { [ "$1" = observe ] || [ "$1" = park ]; }; then
    activity paused "$2" "${reason:-health_guard}"
    case "$reason" in blocked_auth|blocked_quota|blocked_model_usage) route_failure "$2" "$reason" || true ;; esac
  fi
  return "$status"
}
workflow_enabled(){ [ -f "$STATE/workflow.enabled" ]; }
workflow_call(){
  local action="$1" sess="$2"; shift 2
  python3 -m forge.workflow_control "$action" --state "$STATE" --config "$DIR/repos.d" --session "$sess" "$@"
}
workflow_guard(){
  workflow_enabled || return 0
  local generation
  generation=$(session_generation_full "$1") || return 1
  workflow_call guard "$1" --generation "$generation"
}
workflow_delivery(){
  workflow_enabled || return 0
  local generation
  generation=$(session_generation_full "$2") || return 1
  workflow_call "$1" "$2" --generation "$generation"
}
declare -A idle prev owner_alerted empty nudges stale_alerted human_waiting seen_generation last_progress

LABELS=( "ready:0E8A16:eligible for builder" "wip:FBCA04:claimed by builder" "ready-for-review:1D76DB:eligible for reviewer" "changes-requested:D93F0B:reviewer sent back - builder must fix" "ready-for-audit:8E44AD:reviewer passed - auditor may merge" "epic:5319E7:epic parent" "hold:B60205:do NOT auto-pick (overrides ready)" )
BUILD_NUDGE="Re-read ~/ops/forge-watchdog/roles/builder.md. Continue your BUILDER role within the supervisor assignment. Recover only eligible owned PRs and finish your assigned work. Before any new claim, run ~/ops/forge-watchdog/bin/forge next --repo OWNER/REPO using the actual full repository name; claim only its returned issue number. Honor hold and existing claims, and run one issue to a ready-for-review PR. Never infer stale-claim ownership from labels or age. Never merge. Build only via safe-build."
REVIEW_NUDGE="Re-read ~/ops/forge-watchdog/roles/reviewer.md. Continue your REVIEWER role: review the next ready-for-review PR; if it passes, swap its label to ready-for-audit for the security auditor. Never merge, never deploy. Build only via safe-build."
AUDIT_NUDGE="Re-read ~/ops/forge-watchdog/roles/auditor.md. You own the final criticality classification: label material high-risk changes critical and require human approval without bypass; normal audited work must satisfy all repository-required approvals without bypass. Audit the next ready-for-audit PR against the hard-fail rules and full diff, record classification and evidence for the exact candidate, and follow its merge policy. Never deploy."

ensure_clone(){
  local remote
  if [ -d "$2/.git" ]; then
    remote=$(timeout --kill-after=2 10 git -C "$2" remote get-url origin 2>/dev/null) || return 1
    case "$remote" in "git@github.com:$1.git"|"https://github.com/$1.git"|"https://github.com/$1") return 0;; *) glog "clone remote mismatch for $1"; return 1;; esac
  fi
  glog "clone $1 -> $2"
  timeout 120 git clone "git@github.com:$1.git" "$2" >/dev/null 2>&1
}
sess_up(){ tmux has-session -t "=$1" 2>/dev/null; }
sess_client(){
  case "$(tmux display-message -p -t "=$1:1.1" '#{pane_current_command}' 2>/dev/null)" in
    claude|claude.exe) return 0 ;;
    *) return 1 ;;
  esac
}
session_generation(){
  local value
  value=$(tmux display-message -p -t "=$1:1.1" '#{session_id}|#{session_created}|#{pane_id}') || return 1
  [[ "$value" =~ ^\$[0-9]+\|[0-9]+\|%[0-9]+$ ]] || return 1
  printf '%s\n' "$value"
}
session_generation_full(){
  local value
  value=$(tmux display-message -p -t "=$1:1.1" '#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}') || return 1
  [[ "$value" =~ ^\$[0-9]+\|[0-9]+\|%[0-9]+\|[0-9]+$ ]] || return 1
  printf '%s\n' "$value"
}
actual_provider(){
  local provider generation
  provider=$(python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" current --session "$1" 2>/dev/null) || provider=""
  case "$provider" in claude|codex) echo "$provider"; return ;; esac
  generation=$(session_generation_full "$1") || return 1
  provider=$(python3 -m forge.startup codex-observe --state "$STATE" --config "$DIR/repos.d" --session "$1" --generation "$generation" --field provider 2>/dev/null) || provider=""
  if [ "$provider" = codex ]; then echo codex; elif sess_client "$1"; then echo claude; else return 1; fi
}
provider_allowed(){
  local generation
  generation=$(session_generation_full "$1") || return 1
  python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" allowed --session "$1" --provider "$2" --generation "$generation" >/dev/null
}

route_failure(){
  local generation provider
  generation=$(session_generation_full "$1") || return 1
  provider=$(actual_provider "$1") || return 1
  python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" failure --session "$1" --provider "$provider" --reason "$2" --generation "$generation" >/dev/null
}
route_success(){
  local generation
  generation=$(session_generation_full "$1") || return 1
  python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" success --session "$1" --generation "$generation" >/dev/null
}
role_prompt(){
  printf '%s' "Read your role file at $2 and operate strictly per it. If $STATE/provider-handovers/$1.json exists for this session, read that manifest first and compare it with current GitHub and local state. Preserve/recover its still-active exact issue claim and branch, including uncommitted and untracked work, before any new claim. Do not revive closed or superseded claims. Never reset or discard local work. Process only currently eligible work for this dispatch. $3"
}

bind_provider(){
  local generation
  generation=$(session_generation_full "$1") || return 1
  python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" bind --session "$1" --provider "$2" --generation "$generation" >/dev/null
}
send_codex_command(){
  local sess="$1" generation="$2" command="$3" short_generation pane status
  short_generation="${generation%|*}"; pane="${short_generation##*|}"
  provider_allowed "$sess" codex || { glog "[$sess] Codex provider cooldown; command deferred"; return 1; }
  [ "$(session_generation_full "$sess")" = "$generation" ] || return 1
  [ "$(tmux display-message -p -t "=$sess:1.1" '#{pane_current_command}')" = bash ] || return 1
  python3 -m forge.startup is-uncertain --state "$STATE" --session "$sess" --generation "$short_generation"; status=$?
  [ "$status" = 1 ] || return 1
  workflow_guard "$sess" || { glog "[$sess] workflow assignment prevents dispatch"; return 1; }
  workflow_delivery begin-send "$sess" || return 1
  if ! tmux send-keys -t "$pane" -l "$command" || [ "$(session_generation_full "$sess")" != "$generation" ] || ! tmux send-keys -t "$pane" Enter; then
    python3 -m forge.startup dispatch-uncertain --state "$STATE" --session "$sess" --generation "$short_generation" >/dev/null || true
    glog "[$sess] Codex command submission uncertain; input preserved"
    return 1
  fi
  python3 -m forge.startup mark-dispatched --state "$STATE" --session "$sess" --generation "$short_generation" >/dev/null || { glog "[$sess] Codex dispatch marker failed"; return 1; }
  workflow_delivery submitted "$sess" || { glog "[$sess] workflow delivery confirmation missing; preserved as uncertain"; return 1; }
  activity dispatch "$sess" codex_exec_submitted
  return 0
}
watch_codex(){
  local sess="$1" role_file="$2" nudge="$3" generation observation failure command reason metadata
  generation=$(session_generation_full "$sess") || return
  observation=$(python3 -m forge.startup codex-observe --state "$STATE" --config "$DIR/repos.d" --session "$sess" --generation "$generation" --field state) || observation=unknown
  case "$observation" in
    busy) health progress "$sess" >/dev/null || true; return ;;
    blocked)
      failure=$(python3 -m forge.startup codex-observe --state "$STATE" --config "$DIR/repos.d" --session "$sess" --generation "$generation" --field failure) || failure=""
      [ -z "$failure" ] || route_failure "$sess" "$failure" || true
      glog "[$sess] Codex execution blocked: ${failure:-execution_failed}; work preserved"
      return ;;
    idle)
      route_success "$sess" || true
      if [[ "$sess" == *-build ]] && release_worker forge.handoff cleanup "$sess"; then
        glog "[$sess] released completed builder before admitting subsequent work"
        return
      fi
      ;;
    *) glog "[$sess] Codex execution state unknown; no input sent"; return ;;
  esac
  health check "$sess" >/dev/null || return
  # This command refuses unchanged work, missing exact thread identity, pending
  # children, or a nonempty shell. Completed execution is never task completion.
  command=$(python3 -m forge.startup codex-command --config "$DIR/repos.d" --state "$STATE" --session "$sess" --executable "$FORGE_CODEX_BIN" --generation "$generation" --resume --prompt "$(role_prompt "$sess" "$role_file" "$nudge")") || {
    if [[ "$sess" == *-build ]]; then
      # Keep the repeated completion proof as the authoritative retained-slot
      # decision; resume diagnostics must not reset its duration every poll.
      metadata=$(printf '%s' "$command" | python3 -c 'import json,sys; value=json.load(sys.stdin); print(json.dumps(dict(reason=value.get("reason", "codex_preparation_unavailable"), phase="codex_resume", outcome="retained", generation_id=sys.argv[1])))' "$generation") || metadata='{}'
      python3 -m forge.activity --state "$STATE" --config "$DIR/repos.d" event --session "$sess" --event lifecycle_decision --metadata "$metadata" >/dev/null || true
    else
      python3 -m forge.lifecycle --state "$STATE" --config "$DIR/repos.d" decision --session "$sess" --phase codex_resume --generation "$generation" --result "$command" --exit-code 1 >/dev/null || true
    fi
    reason=$(printf '%s' "$command" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason", "codex_preparation_unavailable"))') || reason=codex_preparation_unavailable
    glog "[$sess] Codex resume deferred: $reason"
    return
  }
  send_codex_command "$sess" "$generation" "$command" || true
}

provider_state(){
  local foreground pane
  foreground=$(tmux display-message -p -t "=$1:1.1" '#{pane_current_command}') || { echo unknown; return; }
  if [ "$foreground" = bash ]; then
    local generation
    generation=$(session_generation_full "$1") || { echo unknown; return; }
    python3 -m forge.startup codex-observe --state "$STATE" --config "$DIR/repos.d" --session "$1" --generation "$generation" --field state || echo unknown
    return
  fi
  pane=$(tmux capture-pane -e -p -t "=$1:1.1") || { echo unknown; return; }
  printf '%s' "$pane" | python3 -m forge.activity classify --command "$foreground" --state-only || echo unknown
}
release_worker(){ # module phase session repo(optional)
  local module="$1" phase="$2" sess="$3" repo="${4:-}" provider="${5:-}" result status generation
  local args=(--config "$DIR/repos.d" --state "$STATE" --session "$sess")
  [ -z "$repo" ] || args+=(--repo "$repo")
  [ -z "$provider" ] || args+=(--provider "$provider")
  generation=$(tmux display-message -p -t "=$sess:1.1" '#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}' 2>/dev/null || true)
  result=$(python3 -m "$module" release "${args[@]}"); status=$?
  [ -n "$result" ] || result='{"eligible":false,"released":false,"reason":"verification_unavailable"}'
  python3 -m forge.lifecycle --state "$STATE" --config "$DIR/repos.d" decision --session "$sess" --phase "$phase" --result "$result" --generation "$generation" >/dev/null 2>&1 || glog "[$sess] lifecycle decision write failed"
  return "$status"
}
maybe_handover(){
  local sess="$1" provider result reason
  provider=$(actual_provider "$sess") || return 1
  result=$(python3 -m forge.provider_handover check --config "$DIR/repos.d" --state "$STATE" --session "$sess" --provider "$provider") || return 1
  reason=$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("failure_class", ""))') || return 1
  case "$reason" in blocked_model_usage|blocked_quota|blocked_auth) ;;
    *) return 1 ;;
  esac
  # Persist the provider outage before retiring its old generation, so normal
  # global routing can choose an available harness without retrying the limit.
  route_failure "$sess" "$reason" || { glog "[$sess] provider outage could not be recorded; handover deferred"; return 1; }
  release_worker forge.provider_handover provider_handover "$sess" "" "$provider"
}

submit_task(){ # A dispatch means submitted, never proof that work began.
  local sess="$1" generation="$2" text="$3" reason="$4" current pane uncertain_status
  provider_allowed "$sess" claude || { glog "[$sess] Claude provider cooldown; input deferred"; return 1; }
  current=$(session_generation "$sess") || return 1
  [ "$current" = "$generation" ] && [ "$(provider_state "$sess")" = idle ] || return 1
  python3 -m forge.startup is-uncertain --state "$STATE" --session "$sess" --generation "$generation"; uncertain_status=$?
  if [ "$uncertain_status" -ne 1 ]; then
    glog "[$sess] prior partial dispatch requires reconciliation; input preserved"
    return 1
  fi
  if workflow_enabled; then
    local assignment_text
    assignment_text=$(workflow_call prompt "$sess") || return 1
    text="$assignment_text $text"
  fi
  workflow_guard "$sess" || { glog "[$sess] workflow assignment prevents dispatch"; return 1; }
  workflow_delivery begin-send "$sess" || return 1
  pane="${generation##*|}"
  if ! tmux send-keys -t "$pane" -l "$text"; then
    python3 -m forge.startup dispatch-uncertain --state "$STATE" --session "$sess" --generation "$generation" >/dev/null || true
    glog "[$sess] dispatch uncertain after literal send; input preserved"
    return 1
  fi
  sleep 0.4
  if [ "$(session_generation "$sess")" != "$generation" ] || ! sess_client "$sess" || ! tmux send-keys -t "$pane" Enter; then
    python3 -m forge.startup dispatch-uncertain --state "$STATE" --session "$sess" --generation "$generation" >/dev/null || true
    glog "[$sess] dispatch uncertain before acknowledgement; input preserved"
    return 1
  fi
  if ! python3 -m forge.startup mark-dispatched --state "$STATE" --session "$sess" --generation "$generation" >/dev/null; then
    python3 -m forge.startup dispatch-uncertain --state "$STATE" --session "$sess" --generation "$generation" >/dev/null || true
    activity dispatch "$sess" "$reason"
    glog "[$sess] submitted dispatch receipt failed; automatic resubmission blocked"
    return 1
  fi
  workflow_delivery submitted "$sess" || { glog "[$sess] workflow delivery confirmation missing; preserved as uncertain"; return 1; }
  activity dispatch "$sess" "$reason"
  return 0
}

_nav_prompts(){ local t="$1" p; p=$(tmux capture-pane -p -t "$t" 2>/dev/null||echo "")
  echo "$p"|grep -qi "trust this folder\|Yes, I trust" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 4; p=$(tmux capture-pane -p -t "$t"||echo ""); }
  echo "$p"|grep -qi "fullscreen renderer" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 3; p=$(tmux capture-pane -p -t "$t"||echo ""); }
  echo "$p"|grep -qi "Bypass Permissions mode" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 4; }
}
workspace_available(){
  [ -d "$STATE/execution-retirements" ] || return 0
  local status
  python3 -m forge.execution_retirement quarantined --config "$DIR/repos.d" --state "$STATE" --session "$1" >/dev/null; status=$?
  [ "$status" -eq 1 ]
}
reconcile_finished_workspace(){
  local sess="$1" result
  declare -gA workspace_refusals
  case "$sess" in *-review|*-audit) ;; *) return 0 ;; esac
  workspace_available "$sess" && return 0
  # A published result ends the attempt, even if the issue needs another build.
  # This verifies the preserved checkout under the supervisor lock; it never
  # resets files, bypasses ownership, or retries an unverified result.
  if result=$(python3 -m forge.execution_reconciliation reconcile --config "$DIR/repos.d" --state "$STATE" --session "$sess" --lock-fd 8); then
    unset 'workspace_refusals[$sess]'
    glog "[$sess] published role result reconciled; workspace available for next issue"
  elif [ "${workspace_refusals[$sess]:-}" != "${result:-reconciliation failed without evidence}" ]; then
    workspace_refusals[$sess]="${result:-reconciliation failed without evidence}"
    glog "[$sess] workspace reconciliation deferred: ${workspace_refusals[$sess]}"
  fi
}
retire_finished_worker(){
  local sess="$1" repo="$2"
  sess_up "$sess" || return 0
  case "$(provider_state "$sess")" in idle|blocked) ;; *) return 0 ;; esac
  # Resolve valid task handoffs first. A terminal execution must still be able
  # to release capacity if its task result cannot be verified.
  if [[ "$sess" == *-audit ]] && release_worker forge.human_wait human_wait_cleanup "$sess" "$repo"; then
    glog "[$sess] completed audit released; issue outcome retained"
  elif release_worker forge.handoff cleanup "$sess"; then
    glog "[$sess] verified handoff released"
  elif [ "$(actual_provider "$sess")" = codex ] && release_worker forge.execution_retirement execution_retirement "$sess"; then
    glog "[$sess] terminal execution retired; unresolved work preserved separately from capacity"
  fi
}
launch(){ # sess dir role nudge — fresh bounded legacy context; no inferred continuation
  local sess="$1" dir="$2" role="$3" nudge="$4" t="=$1:1.1" p provider_command generation provider executable full_generation reservation_result
  if ! python3 -m forge.capacity check "${CAP_ARGS[@]}" --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null; then
    glog "[$sess] capacity admission deferred; see state/capacity.json"
    activity admission_deferred "$sess" capacity_guard
    return
  fi
  workspace_available "$sess" || { glog "[$sess] preserved workspace needs attention; other repositories remain eligible"; return; }
  ensure_clone "${REPO}" "$dir" || return
  # A clone may take two minutes: recheck current pressure immediately before launch.
  python3 -m forge.capacity check "${CAP_ARGS[@]}" --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null || { activity admission_deferred "$sess" capacity_recheck; return; }
  if workflow_enabled; then
    reservation_result=$(workflow_call reserve "$sess" 2>&1) || { glog "[$sess] workflow reservation deferred: $reservation_result"; return; }
  fi
  # Reserve a provider probe only after this worker has passed admission.
  provider=$(python3 -m forge.routing --state "$STATE" --config "$DIR/repos.d" select --session "$sess") || { if workflow_enabled; then workflow_call blocked "$sess" --reason provider_unavailable >/dev/null || true; fi; glog "[$sess] no provider available"; return; }
  case "$provider" in
    claude) executable="$FORGE_CLAUDE_BIN" ;;
    codex) executable="$FORGE_CODEX_BIN" ;;
    # Routing only selects qualified harnesses; refuse loudly rather than dispatch an unqualified one.
    grok) glog "[$sess] Grok Build selected but its legacy runner is not qualified; shell not created"; return ;;
    *) glog "[$sess] unknown provider '$provider' selected; shell not created"; return ;;
  esac
  if ! provider_command=$(python3 -m forge.startup command --state "$STATE" --session "$sess" --executable "$executable"); then
    python3 -m forge.startup record-failure --state "$STATE" --session "$sess" >/dev/null 2>&1 || true
    health start_failed "$sess" >/dev/null || true
    activity paused "$sess" provider_start_failed
    glog "[$sess] provider executable unavailable; configure selected provider executable"
    return
  fi
  health launch "$sess" >/dev/null || return
  if [ -f "$STATE/$sess.up" ]; then notify "[forge] WARNING: $sess was up but vanished — relaunching (crash?)"; glog "crash-relaunch $sess"; else glog "wake $sess"; fi
  tmux new-session -d -s "$sess" -c "$dir" || { glog "session launch conflict $sess"; return 1; }; activity session_created "$sess" launch; sleep 2
  generation=$(session_generation "$sess") || { glog "[$sess] startup identity unavailable"; return; }
  if workflow_enabled; then
    full_generation=$(session_generation_full "$sess") || return
    workflow_call bind "$sess" --generation "$full_generation" --provider "$provider" || { glog "[$sess] workflow binding failed; shell preserved without task"; return; }
  fi
  seen_generation[$sess]="$generation"; idle[$sess]=0; nudges[$sess]=0
  last_progress[$sess]=$(python3 -m forge.lifecycle --state "$STATE" --config "$DIR/repos.d" progress --session "$sess" 2>/dev/null) || last_progress[$sess]=""
  if [ "$provider" = codex ]; then
    full_generation=$(session_generation_full "$sess") || return
    provider_command=$(python3 -m forge.startup codex-command --config "$DIR/repos.d" --state "$STATE" --session "$sess" --executable "$executable" --generation "$full_generation" --prompt "$(role_prompt "$sess" "$role" "$nudge")") || { glog "[$sess] Codex startup preparation refused; shell preserved"; return; }
    if send_codex_command "$sess" "$full_generation" "$provider_command"; then bind_provider "$sess" codex || glog "[$sess] Codex provider binding failed"; fi
    return
  fi
  if ! tmux send-keys -t "$t" -l "$provider_command" || ! tmux send-keys -t "$t" Enter; then
    python3 -m forge.startup dispatch-uncertain --state "$STATE" --session "$sess" --generation "$generation" >/dev/null || true
    glog "[$sess] provider command submission uncertain; preserving input"
    return
  fi
  sleep 12
  if ! sess_client "$sess"; then
    python3 -m forge.startup record-failure --state "$STATE" --session "$sess" >/dev/null || true
    health start_failed "$sess" >/dev/null || true
    activity paused "$sess" provider_start_failed
    glog "$sess provider startup failed; diagnostic in state/provider-starts; no task dispatched"
    return
  fi
  _nav_prompts "$t"
  p=$(tmux capture-pane -p -t "$t" 2>/dev/null||echo "")
  if ! printf '%s' "$p" | health observe "$sess" >/dev/null; then
    glog "$sess startup blocked; provider/session cooldown recorded"
    return
  fi
  sleep 3
  sess_client "$sess" || { python3 -m forge.startup record-failure --state "$STATE" --session "$sess" >/dev/null || true; health start_failed "$sess" >/dev/null || true; activity paused "$sess" provider_start_failed; return; }
  [ "$(provider_state "$sess")" = idle ] || { glog "[$sess] provider not yet at a verified empty prompt; dispatch deferred"; return; }
  if submit_task "$sess" "$generation" "$(role_prompt "$sess" "$role" "$nudge")" initial_task; then bind_provider "$sess" claude || glog "[$sess] Claude provider binding failed"; fi
}

watch(){ # Existing sessions must be observed before any input is sent.
  local sess="$1" short="$2" work="$3" nudge="$4" role_file="${6:-}" generation state pane progress progress_seen=0
  if [ "$(actual_provider "$sess")" = codex ]; then watch_codex "$sess" "$role_file" "$nudge"; return; fi
  generation=$(session_generation "$sess") || { glog "[$sess] session identity unknown; input preserved"; return; }
  if [ "${seen_generation[$sess]:-}" != "$generation" ]; then
    seen_generation[$sess]="$generation"; idle[$sess]=0; nudges[$sess]=0; prev[$sess]=""; last_progress[$sess]=""
  fi
  progress=$(python3 -m forge.lifecycle --state "$STATE" --config "$DIR/repos.d" progress --session "$sess" 2>/dev/null) || progress=""
  if [ -n "$progress" ]; then
    if [ -n "${last_progress[$sess]:-}" ] && [ "${last_progress[$sess]}" != "$progress" ]; then
      idle[$sess]=0; nudges[$sess]=0; progress_seen=1
    fi
    last_progress[$sess]="$progress"
  fi
  state=$(provider_state "$sess")
  if [ "$state" = busy ]; then
    health progress "$sess" >/dev/null || true
    [ "$progress_seen" = 0 ] || route_success "$sess" || true
    idle[$sess]=0; nudges[$sess]=0; return
  fi
  if [ "$state" = idle ] && [[ "$sess" == *-build ]] && release_worker forge.handoff cleanup "$sess"; then
    glog "[$sess] released completed builder before admitting subsequent work"
    return
  fi
  if [ "$progress_seen" = 1 ]; then health progress "$sess" >/dev/null || true; fi
  health check "$sess" >/dev/null || return
  pane=$(tmux capture-pane -e -p -t "=$sess:1.1") || return
  if ! printf '%s' "$pane" | health observe "$sess" >/dev/null; then
    glog "[$short] $sess explicit current provider error; cooldown recorded"
    return
  fi
  [ "$state" = idle ] || { idle[$sess]=0; glog "[$sess] provider state $state; input preserved"; return; }
  [ "$progress_seen" = 0 ] || route_success "$sess" || true
  idle[$sess]=$(( ${idle[$sess]:-0} + 1 ))
  if [ "${idle[$sess]}" -ge 2 ]; then
    if [ "${nudges[$sess]:-0}" -ge 2 ]; then
      health park "$sess" >/dev/null || true
      glog "[$short] $sess no observed progress after two delivered nudges; parked"
      nudges[$sess]=0; idle[$sess]=0; return
    fi
    if submit_task "$sess" "$generation" "$nudge" nudge; then
      nudges[$sess]=$(( ${nudges[$sess]:-0} + 1 ))
      activity nudge "$sess" pending_idle
      glog "[$short] $sess idle, $work pending - nudged (${nudges[$sess]}/2)"
    fi
    idle[$sess]=0
  fi
  prev[$sess]="$work"
}

role_reconcile(){ # sess dir role nudge pending short — launch/nudge only when this role has work
  local sess="$1" dir="$2" role="$3" nudge="$4" pending="$5" short="$6" repo="${7:-}"
  # Operator intent is processed under the same lock as dispatch. A stop pauses
  # only this generation; a replacement worker never inherits the pause.
  if [ -d "$STATE/slot-actions" ] && { [ -f "$STATE/slot-actions/pause-$sess.json" ] || [ ! -f "$STATE/slot-actions/.pending-indexed" ] || compgen -G "$STATE/slot-actions/pending/$sess/*.json" >/dev/null; }; then
    python3 -m forge.slot_actions process --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null || { glog "[$sess] operator action failed; inspect slot action receipt"; return; }
    local pause_status
    python3 -m forge.slot_actions paused --state "$STATE" --session "$sess"; pause_status=$?
    [ "$pause_status" -eq 1 ] || return
  fi
  reconcile_finished_workspace "$sess"
  # Preserve the existing provider fallback before parking unresolved execution.
  workspace_available "$sess" || return 0
  if sess_up "$sess" && maybe_handover "$sess"; then
    glog "[$short] verified provider handover preserved existing work; returning to admission"
  fi
  retire_finished_worker "$sess" "$repo"
  reconcile_finished_workspace "$sess"
  workspace_available "$sess" || return 0
  [ "$pending" = "?" ] && return
  if [[ "$sess" == *-build ]] && [ "$pending" -eq 0 ] && ! sess_up "$sess"; then
    local recovery_demand
    recovery_demand=$(python3 -m forge.handover_demand --config "$DIR/repos.d" --state "$STATE" --session "$sess") || recovery_demand=0
    if [[ "$recovery_demand" =~ ^[0-9]+$ ]] && [ "$recovery_demand" -gt 0 ]; then
      pending="$recovery_demand"
      glog "[$short] resuming verified preserved builder claim after provider handover"
    fi
  fi
  # A supervisor restart may interrupt launch before the first task dispatch.
  # Reclaim only a verified old empty shell; never touch claimed or dirty work.
  if [[ "$sess" == *-build || "$sess" == *-review || "$sess" == *-audit ]] && sess_up "$sess" && [ "$(actual_provider "$sess")" != codex ] && ! sess_client "$sess"; then
    if release_worker forge.startup startup_cleanup "$sess"; then
      glog "[$short] recovered interrupted startup; returning to normal admission"
    fi
  fi
  if [[ "$sess" == *-audit ]] && [ "$pending" -gt 0 ] && [ -n "$repo" ]; then
    if python3 -m forge.human_wait check --config "$DIR/repos.d" --state "$STATE" --repo "$repo" >/dev/null; then
      if [ "${human_waiting[$sess]:-0}" != 1 ]; then
        glog "[$short] audit complete; waiting for human approval, no worker slot required"
        human_waiting[$sess]=1
      fi
      if sess_up "$sess"; then
        if release_worker forge.human_wait human_wait_cleanup "$sess" "$repo"; then
          glog "[$short] released verified auditor awaiting human approval"
        fi
      fi
      return
    fi
    human_waiting[$sess]=0
  fi
  [ "$pending" -eq 0 ] && return
  if sess_up "$sess"; then
    watch "$sess" "$short" "$pending" "$nudge" "$dir" "$role"
  else
    health check "$sess" >/dev/null || return
    launch "$sess" "$dir" "$role" "$nudge"
  fi
}

glog "=== supervisor started (demand-loaded; sleep after ${SLEEP_AFTER_EMPTY_POLLS} empty polls) ==="; [ "${FORGE_QUIET_START:-0}" = 1 ] || notify "[forge] supervisor up"
while true; do
  if workflow_enabled; then
    python3 -m forge.workflow_control observe --state "$STATE" --config "$DIR/repos.d" || glog "workflow observation unavailable; existing work preserved"
  fi
  if ! config_records=$(python3 -m forge.config --config "$DIR/repos.d" --home "$HOME" --state "$STATE"); then
    glog "repository configuration invalid; admission paused"
    [ "${FORGE_ONCE:-0}" = 1 ] && break
    sleep "$POLL"
    continue
  fi
  while IFS=$'\t' read -r REPO OWNER_LABEL CLONE_BUILD CLONE_REVIEW CLONE_AUDIT RECOVERY_AUTHOR ENABLED; do
    [ "$ENABLED" = true ] || continue; [ -n "$REPO" ] || continue
    short="${REPO##*/}"
    # One paginated snapshot supplies every stage. Unknown ownership/API state blocks admission.
    read -r bw rw aw wc cw lw bp < <(timeout --kill-after=2 120 "$DIR/bin/forge" snapshot --repo "$REPO" --owner "$OWNER_LABEL" --state "$STATE" --author "${RECOVERY_AUTHOR:-forge-bot}")
    if [ "${bp:-?}" = "?" ]; then
      empty[$short]=0
      glog "[$short] GitHub/ownership state unknown; admission paused"
      continue
    fi
    role_reconcile "$short-build"  "$CLONE_BUILD"  "$DIR/roles/builder.md"  "$BUILD_NUDGE"  "$bp" "$short" "$REPO"
    role_reconcile "$short-review" "$CLONE_REVIEW" "$DIR/roles/reviewer.md" "$REVIEW_NUDGE" "$rw" "$short" "$REPO"
    role_reconcile "$short-audit"  "$CLONE_AUDIT"  "$DIR/roles/auditor.md"  "$AUDIT_NUDGE"  "$aw" "$short" "$REPO"
    # stale-wip sweep: a wip issue untouched >24h is likely an abandoned claim — alert once
    now=$(date +%s)
    while IFS=$'\t' read -r inum iupd; do
      [ -n "$inum" ] || continue
      key="$REPO:$inum"
      its=$(date -d "$iupd" +%s 2>/dev/null || echo "$now")
      stale_key=$(printf '%s' "$key:$iupd" | sha256sum | cut -d' ' -f1)
      if [ $(( now - its )) -gt 86400 ] && [ ! -f "$STATE/stale-$stale_key" ]; then
        glog "[$short] stale wip issue #$inum (no activity >24h; ownership needs reconciliation)"
        if "$DIR/notify.sh" --event-key "stale:$key:$iupd" "[forge] [$short] issue #$inum has wip with no activity for >24h; verify ownership before recovery" >/dev/null; then
          touch "$STATE/stale-$stale_key"
        fi
      fi
    done < <(python3 - "$STATE" "$REPO" <<'SNAPSHOT_PY'
import json, sys
from forge.control import snapshot_path
try:
    for item in json.loads(snapshot_path(sys.argv[1], sys.argv[2]).read_text()).get('stale_candidates', []):
        if item.get('updated_at'):
            print(str(item['number']) + '\t' + item['updated_at'])
except (OSError, ValueError):
    pass
SNAPSHOT_PY
)
    # Empty inventory alone never authorizes termination. Each role's verified
    # completion proof above owns release, including clean checkout and children.
  done <<< "$config_records"
  if workflow_enabled; then
    python3 -m forge.workflow_control observe --state "$STATE" --config "$DIR/repos.d" || glog "workflow observation unavailable; existing work preserved"
  fi
  [ "${FORGE_ONCE:-0}" = 1 ] && break
  sleep "$POLL"
done
