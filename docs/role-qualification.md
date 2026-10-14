# Qualify the installed coding roles

After upgrading an idle test instance, inspect its installed image and coding account:

```sh
forge setup qualify-roles --ssh anvil --repo lucbocahut/dd-frigate --issue 10 --json
```

Add `--apply` to run Build, Review, Audit, Recovery, Triage, Strategist and Discussion serially. The factory and merge settings must be disabled, with no unfinished jobs or account login. The CLI reads configuration itself and runs as the service account using the qualified SQLite runtime. It takes the normal controller lock; it does not activate or pause the factory.

Each role uses the installed image, normal provider harness, immutable assignment, isolated worker network, read-only Git mirror and bounded work volume. The provider must inspect a tracked file and return its path and Git blob identity. The host verifies that identity, a successful mirror fetch, the image execution envelope and the role result schema. Build must export a valid Git bundle containing only a new `forge-qualification.txt`; the host uses the production output verifier. No branch or pull request is published.

The selected repository and issue are real, read-only context. These qualification inputs are explicit fixtures in a separate temporary SQLite database:

- Review and Audit: a candidate whose base equals its head, with a synthetic PR identity.
- Recovery: a synthetic incorrect assertion, with preserved failure evidence.
- Triage: an index containing only the selected issue.
- Build and Discussion: a small repository-reading qualification task.

A qualified receipt proves these execution paths work. It does not prove the selected issue is implemented, a real change passed Review/Audit, publication works, or a merge occurred. Run the separate publication qualification and eventual issue-to-merge test for those claims.

There are at most seven agent invocations, with no automatic retry. Each has a 180-second assignment deadline; the whole run has a 25-minute deadline and transport cleanup margin. Agent invocations can make multiple model requests and incur provider charges. Provider authentication uses the normal private credential path, including OAuth refresh when needed. GitHub credentials remain on the host and cached installation tokens are revoked afterward.

The JSON receipt reports each completed role, exact image/source/provider, verified file identity, mirror requests, fixture limits, cleanup and final automation state. Containers, networks, volumes, sessions, private job secrets and the temporary database are removed after capture. A cleanup failure is reported as unconfirmed and retains the temporary evidence path; it never produces a qualified CLI result. No real provider run is performed by tests of this command.
