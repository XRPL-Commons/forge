# Operator UI rules

Forge owns its implementation details. The UI presents decisions about the work
an operator wants the factory to do.

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
- Give each visible setting a plain name and a clear effect. Offer model or effort
  controls only when the harness contract supports and validates those choices.
- Preserve existing settings and unrelated configuration when saving a focused
  operator choice. Detect conflicting edits instead of silently overwriting them.
