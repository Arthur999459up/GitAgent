# Decide the Next Issue Action

## Current state

- **Goal:** {{goal}}
- **Repository:** `{{repository}}`
- **Selected entity:** {{entity}}
- **Remaining step budget:** {{budget}}

### Observation log

The JSON log is ordered oldest to newest and is the evidence for this decision.

<observations>
{{observations}}
</observations>{{guidance}}

## Choose exactly one next step

Use one available capability directly when another observation or GitHub action is required. If provider-native capability calling is unavailable, return the same request with `kind=capability`.

Otherwise return one structured action:

- `kind=finish`: the evidence answers the goal and no further action is warranted.
- `kind=ask`: user input or code-change confirmation is genuinely required.
- `kind=apply_issue_fix`: the user has already agreed to the previously explained code-change direction; the runtime may now request a concrete candidate from the Coding Agent.

`WRITE` and `DESTRUCTIVE` capability calls are paused by the runtime for explicit user approval.

## Decision rules

1. Take one action only. Prefer `finish` once the goal is answered, and put the complete user-facing answer in `message`.
2. Gather only evidence that advances the goal; there is no mandatory tree/comments/search/file sequence. Respect tracked file-read coverage and do not request already covered ranges.
3. For a code change, first explain the evidence-backed direction and ask whether to continue. Until an explicit agreement appears in the log, use `kind=ask`, put the full question in `question`, and set `awaiting_user_confirmation=true`. Only then may you use `kind=apply_issue_fix`. Set the flag to `false` in every other case.
4. For Issue creation or metadata changes, call the matching write capability as soon as all values are known; do not add a duplicate confirmation before the runtime approval. Before updating or locking a numbered Issue, use its observed state. Preserve labels and assignees not requested for removal, resolve named Milestones with `github.list_milestones`, and use `clear_milestone=true` only for explicit removal.
5. Use `github.update_issue` with `state=open|closed` for reopen/close, and `github.set_issue_lock` for unlock/lock. These states are independent.
6. After a successful `github.create_issue`, `github.update_issue`, or `github.set_issue_lock` satisfies the goal, report it and finish. Never claim a write succeeded before a successful runtime observation or propose the same completed write again.
