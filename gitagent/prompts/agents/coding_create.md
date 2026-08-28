# Generate One Candidate File

## Fixed context

- **Repository:** `{{repository}}`
- **Change request:** {{description}}
- **Locked file operation:**

<operation>
{{operation}}
</operation>

## Current file

The block is empty for an `ADD` operation.

<current_file>
{{current_content}}
</current_file>

## Repository evidence

<evidence>
{{evidence}}
</evidence>

## Constraints

- Implement only the locked path and action; do not substitute a different file or redesign unrelated code.
- For `MODIFY`, preserve unrelated behavior and follow conventions visible in the current file and evidence. For `ADD`, produce a complete new file.
- Do not invent dependencies, APIs, or project conventions unsupported by the supplied context.
- Return only the complete final UTF-8 file content. Do not use a Markdown code fence, add metadata or commentary, or defer the work.{{guidance}}
