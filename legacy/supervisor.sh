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
notify(){ "$DIR/notify.sh" "$1" >/dev/null 2>&1 || glog "notification enqueue FAILED"; }
activity(){ python3 -m forge.activity --state "$STATE" --config "$DIR/repos.d" event --session "$2" --event "$1" --reason "${3:-supervisor}" >/dev/null || glog "[$2] activity ledger write failed: $1"; }
health(){
  local reason status
  reason=$(python3 -m forge.legacy_health "$1" --state "$STATE" --session "$2"); status=$?
  [ -z "$reason" ] || printf '%s\n' "$reason"
  if [ "$status" -ne 0 ] && { [ "$1" = observe ] || [ "$1" = park ]; }; then activity paused "$2" "${reason:-health_guard}"; fi
  return "$status"
}
declare -A idle prev owner_alerted empty nudges stale_alerted human_waiting

LABELS=( "ready:0E8A16:eligible for builder" "wip:FBCA04:claimed by builder" "ready-for-review:1D76DB:eligible for reviewer" "changes-requested:D93F0B:reviewer sent back - builder must fix" "ready-for-audit:8E44AD:reviewer passed - auditor may merge" "epic:5319E7:epic parent" "hold:B60205:do NOT auto-pick (overrides ready)" )
BUILD_NUDGE="Re-read ~/ops/forge-watchdog/roles/builder.md. Continue your BUILDER role within the supervisor assignment. Recover only eligible owned PRs and finish your assigned work. Before any new claim, run ~/ops/forge-watchdog/bin/forge next --repo OWNER/REPO using the actual full repository name; claim only its returned issue number. Honor hold and existing claims, and run one issue to a ready-for-review PR. Never infer stale-claim ownership from labels or age. Never merge. Build only via safe-build."
REVIEW_NUDGE="Re-read ~/ops/forge-watchdog/roles/reviewer.md. Continue your REVIEWER role: review the next ready-for-review PR; if it passes, swap its label to ready-for-audit for the security auditor. Never merge, never deploy. Build only via safe-build."
AUDIT_NUDGE="Re-read ~/ops/forge-watchdog/roles/auditor.md. You own the final criticality classification: label material high-risk changes critical and require human approval without bypass; normal audited work must satisfy all repository-required approvals without bypass. Audit the next ready-for-audit PR against the hard-fail rules and full diff, record classification and evidence for the exact candidate, and follow its merge policy. Never deploy."

ensure_clone(){
  local remote
  if [ -d "$2/.git" ]; then
    remote=$(git -C "$2" remote get-url origin 2>/dev/null) || return 1
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
sess_busy(){ tmux capture-pane -p -t "=$1:1.1" 2>/dev/null | grep -q "esc to interrupt"; }

_nav_prompts(){ local t="$1" p; p=$(tmux capture-pane -p -t "$t" 2>/dev/null||echo "")
  echo "$p"|grep -qi "trust this folder\|Yes, I trust" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 4; p=$(tmux capture-pane -p -t "$t"||echo ""); }
  echo "$p"|grep -qi "fullscreen renderer" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 3; p=$(tmux capture-pane -p -t "$t"||echo ""); }
  echo "$p"|grep -qi "Bypass Permissions mode" && { tmux send-keys -t "$t" Down; sleep 0.3; tmux send-keys -t "$t" Enter; sleep 4; }
}
launch(){ # sess dir role nudge — fresh bounded legacy context; no inferred continuation
  local sess="$1" dir="$2" role="$3" nudge="$4" t="=$1:1.1" p provider_command
  if ! provider_command=$(python3 -m forge.startup command --state "$STATE" --session "$sess" --executable "$FORGE_CLAUDE_BIN"); then
    python3 -m forge.startup record-failure --state "$STATE" --session "$sess" >/dev/null 2>&1 || true
    health start_failed "$sess" >/dev/null || true
    activity paused "$sess" provider_start_failed
    glog "[$sess] provider executable unavailable; configure FORGE_CLAUDE_BIN"
    return
  fi
  if ! python3 -m forge.capacity check "${CAP_ARGS[@]}" --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null; then
    glog "[$sess] capacity admission deferred; see state/capacity.json"
    activity admission_deferred "$sess" capacity_guard
    return
  fi
  ensure_clone "${REPO}" "$dir" || return
  # A clone may take two minutes: recheck current pressure immediately before launch.
  python3 -m forge.capacity check "${CAP_ARGS[@]}" --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null || { activity admission_deferred "$sess" capacity_recheck; return; }
  health launch "$sess" >/dev/null || return
  if [ -f "$STATE/$sess.up" ]; then notify "[forge] WARNING: $sess was up but vanished — relaunching (crash?)"; glog "crash-relaunch $sess"; else glog "wake $sess"; fi
  tmux new-session -d -s "$sess" -c "$dir" || { glog "session launch conflict $sess"; return 1; }; activity session_created "$sess" launch; sleep 2
  tmux send-keys -t "$t" -l "$provider_command"; tmux send-keys -t "$t" Enter; sleep 12
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
  if tmux send-keys -t "$t" -l "Read your role file at $role and operate strictly per it. You were (re)started by the supervisor — process currently eligible work for this dispatch. $nudge"; then
    sleep 0.5
    if tmux send-keys -t "$t" Enter; then activity dispatch "$sess" initial_task; else glog "[$sess] initial dispatch failed"; fi
  fi
  touch "$STATE/$sess.up"
}
teardown(){
  if tmux has-session -t "=$1" 2>/dev/null; then
    tmux kill-session -t "=$1" || return
    activity session_stopped "$1" repository_empty
    glog "sleep $1"
  fi
  rm -f "$STATE/$1.up"
}

watch(){ # sess short pending nudge
  local sess="$1" short="$2" work="$3" nudge="$4" t="=$1:1.1"
  health check "$sess" >/dev/null || return
  if ! sess_client "$sess"; then
    health park "$sess" >/dev/null || true
    glog "[$short] $sess foreground is not Claude; parked without nudge"
    return
  fi
  local pane
  pane=$(tmux capture-pane -p -t "$t" -S -30 2>/dev/null || true)
  if ! printf '%s' "$pane" | health observe "$sess" >/dev/null; then
    glog "[$short] $sess explicit error; cooldown recorded"
    notify "[forge] [$short] $sess blocked; automatic retries paused for one hour"
    return
  fi
  if sess_busy "$sess"; then idle[$sess]=0; nudges[$sess]=0
  else idle[$sess]=$(( ${idle[$sess]:-0} + 1 ))
    if [ "${idle[$sess]}" -ge 2 ]; then
      nudges[$sess]=$(( ${nudges[$sess]:-0} + 1 ))
      if [ "${nudges[$sess]}" -ge 3 ]; then
        # Quiet terminal is not proof of a dead process. Park, do not create a replacement writer.
        health park "$sess" >/dev/null || true
        glog "[$short] $sess no progress after bounded nudges; parked for one hour"
        notify "[forge] [$short] $sess has pending work without visible progress; parked for one hour"
        nudges[$sess]=0; idle[$sess]=0; return
      fi
      sess_client "$sess" || { health park "$sess" >/dev/null || true; return; }
      if tmux send-keys -t "$t" -l "$nudge"; then
        sleep 0.4
        if tmux send-keys -t "$t" Enter; then
          activity nudge "$sess" pending_idle
          activity dispatch "$sess" nudge
          glog "[$short] $sess idle, $work pending - nudged (${nudges[$sess]}/3)"
        fi
      fi
      idle[$sess]=0
    fi
  fi
  local p="${prev[$sess]:-}"; [ -n "$p" ] && [ "$work" != "$p" ] && glog "[$short] $sess pending: $work (was $p)"; prev[$sess]="$work"
}

role_reconcile(){ # sess dir role nudge pending short — launch/nudge only when this role has work
  local sess="$1" dir="$2" role="$3" nudge="$4" pending="$5" short="$6" repo="${7:-}"
  [ "$pending" = "?" ] && return
  # A supervisor restart may interrupt launch before the first task dispatch.
  # Reclaim only a verified old empty shell; never touch claimed or dirty work.
  if [[ "$sess" == *-build || "$sess" == *-review || "$sess" == *-audit ]] && sess_up "$sess" && ! sess_client "$sess"; then
    if python3 -m forge.startup release --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null; then
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
        if python3 -m forge.human_wait release --config "$DIR/repos.d" --state "$STATE" --repo "$repo" --session "$sess" >/dev/null; then
          glog "[$short] released verified auditor awaiting human approval"
        fi
      fi
      return
    fi
    human_waiting[$sess]=0
  fi
  if [ "$pending" -eq 0 ]; then
    # Verified handoffs and completed role queues no longer need a worker slot.
    # The verifier rechecks GitHub, the clone and exact idle process identity.
    if [[ "$sess" == *-build || "$sess" == *-review || "$sess" == *-audit ]] && sess_up "$sess"; then
      if python3 -m forge.handoff release --config "$DIR/repos.d" --state "$STATE" --session "$sess" >/dev/null; then
        glog "[$short] released idle worker after verified PR handoff"
      fi
    fi
    return
  fi
  health check "$sess" >/dev/null || return
  if sess_up "$sess"; then watch "$sess" "$short" "$pending" "$nudge"; else launch "$sess" "$dir" "$role" "$nudge"; fi
}

glog "=== supervisor started (demand-loaded; sleep after ${SLEEP_AFTER_EMPTY_POLLS} empty polls) ==="; [ "${FORGE_QUIET_START:-0}" = 1 ] || notify "[forge] supervisor up"
while true; do
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
    read -r bw rw aw wc cw lw bp < <("$DIR/bin/forge" snapshot --repo "$REPO" --owner "$OWNER_LABEL" --state "$STATE" --author "${RECOVERY_AUTHOR:-forge-bot}")
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
    # sleep when the repo's queue is empty (no builder work incl. limbo/sent-back PRs, no wip, no ready-for-review) and neither session is busy
    busy=0; sess_busy "$short-build" && busy=1; sess_busy "$short-review" && busy=1; sess_busy "$short-audit" && busy=1
    if [ "$bp" = "?" ] || [ "$rw" = "?" ] || [ "$aw" = "?" ] || [ "$wc" = "?" ]; then empty[$short]=0            # gh hiccup: don't sleep on uncertain data
    elif [ "$bp" -gt 0 ] || [ "$rw" -gt 0 ] || [ "$aw" -gt 0 ] || [ "$wc" -gt 0 ] || [ "$busy" = 1 ]; then empty[$short]=0   # work or busy: stay awake
    else
      empty[$short]=$(( ${empty[$short]:-0} + 1 ))
      if [ "${empty[$short]}" -ge "$SLEEP_AFTER_EMPTY_POLLS" ] && { sess_up "$short-build" || sess_up "$short-review" || sess_up "$short-audit"; }; then
        glog "[$short] queue empty ${empty[$short]} polls — sleeping sessions"; teardown "$short-build"; teardown "$short-review"; teardown "$short-audit"; empty[$short]=0
      fi
    fi
  done <<< "$config_records"
  [ "${FORGE_ONCE:-0}" = 1 ] && break
  sleep "$POLL"
done
