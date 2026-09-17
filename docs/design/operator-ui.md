# Operator UI rules

Forge owns its implementation details. The UI presents decisions about the work
an operator wants the factory to do.

- Keep the Forge logo as the home link. Offer one destination link in the top
  bar: Settings from the factory, and Factory from settings, alongside Help. Group General,
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
  page. Never animate a queue count. Do not duplicate factory status or
  saved-draft messages in the header.
- Settings tabs share one content width, left edge, top spacing and title style.
  Help uses the same outer spacing with a reading column and topic sidebar.
