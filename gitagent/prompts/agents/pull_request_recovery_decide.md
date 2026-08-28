# Recover from a Pull Request Capability Failure

## Current state

- **Goal:** {{goal}}
- **Repository:** `{{repository}}`
- **Selected entity:** {{entity}}
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
2. Preserve an explicitly selected PR as the target. Other PRs from list or search results are evidence, never silent replacement targets.
3. Request only missing evidence. Do not repeat an equivalent successful call unless later evidence made it stale, or an unchanged failed call unless new evidence gives a concrete reason it may now succeed.
4. A direct `resource_not_found` may be cross-checked with one broader list/search when that materially improves confidence; stop once the evidence supports an answer.
5. After `execution_uncertain`, do not repeat the mutation. First use `READ` evidence to establish remote state; any later mutation is a new proposal under the normal approval flow.
6. A `finish` action must place the complete evidence-grounded answer in `message` and never claim an unobserved write succeeded.
