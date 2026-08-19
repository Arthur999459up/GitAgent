Issue: {{issue}}
Repository evidence and conversation: {{observations}}
{{guidance}}

Prepare a concise implementation guide for the Coding agent after the user agreed to proceed. Return only:
- description: root cause, intended code change, and required regression coverage in concrete terms.
- target_files: the repository paths that should be inspected/edited. Reuse paths supported by the Issue/repository evidence; include a relevant test file when the evidence clearly identifies it.
- suggested_title: a short Draft PR title.

Do not invent file contents or claim the patch is already applied.
