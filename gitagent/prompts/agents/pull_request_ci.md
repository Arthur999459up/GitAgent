# Analyze Pull Request CI

## Request

{{request}}

## CI and change evidence

<evidence>
{{evidence}}
</evidence>

## Analysis rules

- `facts`: report observed workflow, job, and log facts only, including missing or unavailable logs when relevant.
- `suspected_causes`: label hypotheses as inference and tie them to specific log evidence where possible.
- `related_changes`: connect failures to the supplied Diff, changed files, tests, or dependency changes only when the evidence supports the relationship.
- `actions`: propose focused diagnostic or corrective next steps; do not claim they were run.

Never upgrade an inference into a confirmed fact. If the direct log or test evidence needed to establish a cause is unavailable, preserve that uncertainty explicitly. Do not collapse missing evidence into a diagnosis or imply CI passed when results are incomplete.
