# Operator UI rules

Forge owns its implementation details. The UI presents decisions about the work
an operator wants the factory to do.

- Keep the Forge logo as the home link. Keep Settings in the top bar alongside Help,
  highlighted within settings; the logo returns to the factory. Group General,
  GitHub access and Coding accounts under the Settings tabs. Use the green
  SETTINGS eyebrow above each settings page title; avoid duplicate navigation.
- Show useful choices: managed repositories, worker capacity, model preferences,
  and effort levels supported by the selected coding harness.
- Do not expose credential references, App or installation IDs, file paths,
  environment variables, internal timeouts, or raw JSON as settings. Use guided
  setup and let Forge manage those details.
- Start with the normal workflow. Do not add advanced controls for hypothetical
  maintenance scenarios. Put diagnostic detail in help or technical documentation.
- Distinguish observed state from instructions. Status shows what happened and
  what is running. Blue callouts labeled Help explain how to act; they must not
  look like failures or health indicators. Do not rely on color alone.
- Use quiet pill-shaped controls for internal navigation. Keep established
  breadcrumbs, menus and linked record titles in their existing navigation style.
  Do not style a link that opens a page or detail panel as a primary action.
  Reserve primary emphasis for the main state-changing decision; Return to
  backlog and Pause are secondary actions.
- Render issue descriptions as sanitized Markdown, with readable headings, lists,
  links, code and tables.
- Use solid color with contrasting text for primary actions. Keep secondary
  actions visually quieter, and disabled controls visibly muted.
- Give each visible setting a plain name and a clear effect. Offer model or effort
  controls only when the harness contract supports and validates those choices.
- Preserve existing settings and unrelated configuration when saving a focused
  operator choice. Detect conflicting edits instead of silently overwriting them.

## Mobile is part of every UI change

- Verify affected pages at 320 px and 390 px wide, as well as desktop. Use
  populated, empty, loading and error states where the change affects them.
- Keep primary actions and status readable without horizontal page scrolling.
  Stack columns and adapt repository tables into labeled cards on small screens;
  keep the automation state and action near the repository name. Preserve table
  semantics for assistive technology.
- Wrap long repository names, App names, messages and commands. Use horizontal
  scrolling only for content that needs it, such as wide code, never to conceal
  an operator action off-screen.
- Give touch controls at least 44 px of height and use at least 16 px text in
  mobile form fields. Support keyboard focus, browser zoom and reduced motion.
- Keep navigation reachable, dialogs inside the viewport, and sticky elements
  from covering focused controls or anchor targets. Do not hide functionality on
  mobile; simplify the layout instead.

- Header activity uses compact role-colored spinners with running worker counts,
  plus a count of tracked issues queued for enabled repositories. Hide zero
  counts and stale or unavailable observations; keep diagnostics on the Factory
  page. Exclude internal recovery workers from header activity. Never animate a queue count. Do not duplicate factory status or
  saved-draft messages in the header.
- Settings tabs share one content width, left edge, top spacing and title style.
  Help uses the same outer spacing with a reading column and topic sidebar.

- App cards put the role and App name together beside the illustration, followed
  by its purpose. Keep connection status, repository count and access action compact.
- Normal repository rows keep the same height whether automation is enabled or
  disabled. Use quiet Set up pills for navigation and retain mobile tap targets.
- Verified coding accounts show a green check and no reconnect action. Show a
  primary Connect or Verify connection action only when it is needed.
- Use an accessible question-mark Help icon in the shared header.

- Refresh is additive: preserve the last observation, search, focus and layout while
  checking. Use the shared ForgeRefresh poller and in-place renderer. Keep polling
  non-overlapping and pause it in hidden tabs. Show motion only in an explicit
  refresh control or while the server is checking, not on every background read.
- Use circular-arrow icon buttons for manual rechecks, with contextual Refresh
  accessible labels and tooltips. Animate the icon while the check is running.
  Keep previous results visible after network failure, alongside a freshness warning. Historical evidence must never grant
  permission to dispatch, merge or download a newly verified configuration.

- Show Save and Discard changes only when settings have unsaved edits. Avoid
  a persistent reload control. Repository state icons and actions replace redundant
  Enabled / Not enabled text; retain explanations for states needing attention.
- Nested settings pages use the green SETTINGS breadcrumb above their title.
  Avoid duplicate back controls and reserve primary actions for substantive work.


## Issue decisions

Activity is the default home page and first work tab. It shows running workers
and diagnostic output. Issues is the worklist, available from the second tab.
Show open issues from enabled repositories, including work without a ready label.
Reading or refreshing the inventory must never admit work. Search and repository
and state filters preserve focus, with completed work available separately.

Keep rows to issue identity, a plain-language state and reason, and one next step.
Open details inside Forge for the description, prerequisite issue links, pull
requests and recent decisions. Queue, Return to backlog, Pause, Release hold,
Recheck setup, Retry attempt and Stop attempt appear only where relevant.
Confirm effects before applying a decision. Require a reason for pauses, hold
releases, retries and stops. Explain that stopping discards unfinished workspace
changes. A setup recheck preserves history and the failure budget; Retry grants
a new attempt budget and belongs only to a failed attempt.

Recheck GitHub state, access, repository rules, explicit local dependencies and
existing work before admission-changing decisions. Preserve unrelated labels.
Stale inventories cannot authorize an action. Keep interrupted decisions durable
and block dispatch until their outcome is confirmed. Do not imply priority affects
execution while the scheduler uses creation order.
- Worker capacity is automatic from host memory; operators set only per-role maxima.
  Describe memory waits as queued work that retries automatically, not an issue failure.

## Live Factory dashboard

- Keep the Factory heading, statistics and stage columns in stable positions.
  Show queued issues, productive workers by role, attention count and tracked
  merges. Investigations and cleanup remain separate from productive activity.
- A running count requires fresh supervisor and worker observations. Measure work
  duration from the container start time, never the issue's discovery time or a
  browser refresh. Update timers locally between polls.
- Running issue cards have restrained moving waves along their top and bottom
  edges, in their role's color. Queued, blocked, starting and stale cards do not
  animate. Reduced motion uses a static active border.
- Preserve each card's DOM identity across polls and stage transitions, including
  focused controls and open detail panels. Only actual stage changes animate;
  resizing the viewport must cancel movement rather than displace cards.
- Keep the last known counts and cards after a failed refresh. Label them as last
  observed, stop motion and freeze elapsed times until fresh evidence returns.
- Translate internal reasons into plain language and an available next action.
  For example, blocked_auth identifies the account needing sign-in and links to
  Coding accounts, while the issue remains queued for an available account.
