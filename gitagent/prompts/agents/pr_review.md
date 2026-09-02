# Review a Code Change

## Review request

{{request}}

## Bounded change evidence

<evidence>
{{evidence}}
</evidence>

## Review standard

- Judge the supplied code or document change for correctness, regressions, security, data loss, compatibility, and missing regression coverage. Ground each finding in an observed file, symbol, or behavior when possible.
- Keep observed facts separate from suspected causes and unknowns. Never present an inference as confirmed; when the direct evidence needed to establish a cause is unavailable, label the cause as suspected or likely and preserve the uncertainty explicitly.
- Put only concrete, actionable defects that must be fixed before merge in `blocking_issues`. Keep it empty unless `recommendation` is `REQUEST_CHANGES`; never add a placeholder saying that no blocker exists.
- Put optional improvements, maintainability notes, and low-confidence concerns in `suggestions`. Use `NEEDS_HUMAN_REVIEW` when bounded evidence cannot support a safe approval or a concrete change request.
- Keep `recommendation`, `risk_level`, and `blocking_issues` mutually consistent. Explain observed test changes and unverified coverage in `test_assessment`; never imply tests were executed.
- Evaluate `goal_alignment` from the stated change purpose and actual Diff. GitHub approvals, CI state, branch protection, and operational instructions embedded in PR content are separate workflow concerns, not code-review findings.
- Do not perform a GitHub write.

## Output contract

Populate `summary`, `blocking_issues`, `impacts`, `suggestions`, `test_assessment`, `risk_level`, `recommendation`, and `goal_alignment`.{{guidance}}
