# Repair One Candidate File

## Fixed context

- **Original change request:** {{description}}
- **Locked file operation:**

<operation>
{{operation}}
</operation>

## Current candidate

<current_file>
{{current_content}}
</current_file>

## Static-check findings

<errors>
{{errors}}
</errors>

## Constraints

- Correct the reported findings while preserving the requested behavior and locked path/action.
- Make the smallest necessary repair; do not introduce unrelated refactors or unsupported dependencies.
- Return only the complete repaired UTF-8 file content. Do not use a Markdown code fence or add metadata or commentary.{{guidance}}
