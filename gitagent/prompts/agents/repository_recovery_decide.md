# Recover from a Repository Capability Failure

## Current state

- **Goal:** {{goal}}
- **Repository:** `{{repository}}`
- **Operation:** `{{operation}}`
- **Remaining step budget:** {{budget}}

### Observation log

The JSON log is ordered oldest to newest. Failed calls and later recovery results belong to the same history.

<observations>
{{observations}}
</observations>{{guidance}}

## Choose exactly one action

- `kind=capability`: one additional, relevant observation is still required.
- `kind=ask`: progress genuinely depends on user input.
- `kind=finish`: existing evidence is sufficient to answer the goal.

## Decision rules

1. Take one action only and prefer `finish` as soon as the goal is resolved.
2. Request only missing evidence. Do not repeat an equivalent successful read unless later evidence made it stale, or an unchanged failed call unless new evidence gives a concrete reason it may now succeed.
3. Keep recovery bounded. Never turn one failed read into open-ended exploration or treat incomplete/truncated retrieval as proof of absence.
4. After `execution_uncertain`, do not repeat the mutation. First use `READ` evidence to establish remote state; any later mutation is a new proposal under the normal approval flow.
5. A `finish` action must place the complete user-facing answer in `message`, distinguish evidence from inference, and never claim an unobserved write succeeded.
