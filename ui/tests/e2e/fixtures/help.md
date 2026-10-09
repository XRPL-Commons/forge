# Forge setup help

This is a fixed help document served by the e2e mock in place of the live
`docs/onboarding.md`, so help-related specs never depend on that file's
real content changing.

![Four role cubes](assets/setup-hero.svg)

![External tracking image](https://help-image.invalid/tracker.svg)

## Host installation

Install the Forge worker host, register the systemd unit and confirm the
Docker engine is reachable before starting Onboarding.

## GitHub App creation

Create the four GitHub Apps (build, review, audit, controller) via the
manifest flow and record their installation IDs.

## GitHub repository protection

Enable branch protection on the target repository's default branch and
import the Forge review ruleset.

## Coding provider accounts

Connect at least one coding provider account (Claude or Codex) and verify
its model access.

## Verifying the setup

Run the built-in verification checks from Onboarding Step 3 before
activating the factory.

## Troubleshooting

Common failure modes during setup and how to recover from each one.
