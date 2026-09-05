# Analyze Pull Request Review Dialogue

## Request

{{request}}

## Reviews, comments, and code evidence

<evidence>
{{evidence}}
</evidence>

## Requirements

- Classify each material point by its current status: `resolved`, `explained`, `needs_changes`, `discussion`, or `conflicts`.
- Evaluate whether a comment still applies to the observed Diff; do not mark it resolved merely because it is old or answered.
- Identify contradictions between Reviews or between comments and current code in `conflicts`.
- Produce a concise `reply_draft` grounded in the evidence and suited to the user's request. Do not publish it or claim it was published.
- When evidence is insufficient to determine status, place the point in `discussion` rather than guessing.
