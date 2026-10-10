# Talking to a worker

Use **Discuss blocker** to ask about an issue without starting another implementation attempt. A worker reads the saved evidence, explains what it found and may offer a decision; only your explicit **Apply this decision** asks Forge to change anything.

```mermaid
sequenceDiagram
  actor You
  participant Forge
  participant Worker
  You->>Forge: Ask about this issue
  Forge-->>You: Finding someone
  Forge->>Worker: Start a separate read-only discussion
  Worker-->>You: Ember, Bolt or Nova joins
  Worker-->>You: Activity, then an answer
  You->>Forge: Apply this decision, if offered
  Forge->>Forge: Check current issue and proposal
  Forge-->>You: Submitted, applied or rejected
```

![Three friendly worker cubes gather around a question bubble at the workbench.](assets/discussion-hero.svg)

## Meet the workers

![Persona cards: Ember uses Claude and has a terracotta cube with a spark; Bolt uses Codex and has a teal cube with a lightning mark; Nova uses Grok and has a violet cube with a star.](assets/personas.svg)

**Ember** is Claude, **Bolt** is Codex and **Nova** is Grok. The name tells you which provider is answering; it does not mean the same process has been running between messages.

Forge prefers the last attempt's provider when it is available. Another connected provider can step in, and the conversation shows the change.

## Ask a useful question

Open an issue and choose **Discuss blocker**, or open its **Discussion** tab. Ask one concrete question, such as “Which test failed?”, “What needs a human check?” or “What would a different approach change?”

The worker gets the issue, current candidate, recent attempt summaries and recent conversation. This is reconstructed context, not a live attachment to the original worker; it cannot recover an unsaved private conversation.

![Illustrative Discussion panel: the provider name identifies the speaker, activity explains the wait, and Apply this decision is separate from the answer.](assets/discussion-screen.svg)

While an answer is on its way:

- **Finding someone** means no discussion worker has started yet. The reason may be capacity, productive work going first, a pause or an account cooldown.
- A named worker **joins** when its container starts. One live line shows what it is doing now, updated in place.
- The answer replaces that line. A worker that left before someone else answered leaves no trace.
- If a worker leaves and nobody answers, the thread says why. Use **Try again** when offered; an interrupted answer is not a completed decision.

You can leave and return to the saved conversation. Older messages remain readable, although only a bounded recent portion is sent with the next question.

## Apply a proposal deliberately

```mermaid
flowchart TD
  Q[Question and answer] --> P{Proposal offered?}
  P -->|No| Read[Read or ask a follow-up]
  P -->|Yes| Choice{Apply this decision?}
  Choice -->|No| Keep[Conversation only]
  Choice -->|Yes| Check{Still valid for the current issue?}
  Check -->|Yes| Submit[Submit through normal issue actions]
  Check -->|No| Refresh[Refresh and discuss the changed issue]
```

Read the proposed action and reason before applying it. A submitted action can still be pending or rejected: the controller checks the current issue, requirements and permissions again.

If the issue or pull request changed, an old proposal may no longer apply. Refresh the issue and ask about the new evidence instead of repeatedly submitting the old decision.

If the agent proposes something Forge does not offer on this issue, its answer still arrives, without a button, noting that the action isn't available here.

## Close an issue from the conversation

Whenever the issue can be closed, **Close issue** is one of the decisions the agent may propose, with a one-sentence reason. Its card opens the usual Close dialog with that reason filled in; nothing closes until you confirm.

A direct command such as “close this issue” or “drop this issue” records a close request under your identity. Forge also recognizes “please close this issue”, “could you close this issue?” and “can you please close this issue?”. These are exact command forms, with optional final punctuation; questions such as “how do I close this issue?”, conditional requests and other wording remain proposals for you to confirm.

The reply shows **Close requested** while the controller stops work and closes the issue and its Forge pull request. **Closed** appears only after GitHub confirms the action. A rejected request shows its reason. The closing comment uses the reason you confirmed.

**Undo** is offered only for the unchanged closure Forge confirmed. It records a separate request to reopen the issue; **Reopened** appears after GitHub confirms that request. A later reopen or closure by someone else prevents Undo of the earlier closure. Undo does not reopen the pull request or restore work and budgets. Forge requests a fresh observation so the issue can return to the board. No other discussion proposal is applied automatically.

Discussion does not approve a merge, waive a security check or edit the repository. Its separate, read-only worker does not consume the issue's failure attempts, but does use provider capacity and usage.

## If nobody joins

Check the displayed reason first. Resume Forge if paused, repair an account in **Settings → Coding accounts**, or wait for the shown cooldown or available memory.

Only one discussion worker runs at a time, and productive pipeline work gets priority. A queued or running discussion has a ten-minute limit; a timeout or restart is recorded in the thread so an unanswered question does not appear successful.

For what happens after a decision, see [How an issue moves](issue-state-machine.md). For capacity and credentials, see [Workers, accounts and merging](factory-runtime.md); developers can follow the [conversation implementation reference](issue-conversations-reference.md).
