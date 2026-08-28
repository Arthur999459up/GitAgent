# `gitagent/prompts` — LLM-facing templates

This directory contains the Markdown templates used by GitAgent's domain and worker agents. Keeping behavioral wording here makes prompts reviewable and tunable without mixing it into orchestration code. `PromptLibrary` loads every template once at startup and performs strict `{{placeholder}}` substitution for dynamic content.

## Directories

- `system/`: role, operating principles, and safety boundaries for domain and worker agents.
- `agents/`: task inputs, decision rules, and output contracts.
- `approval/`: classification prompts for a user turn about an open proposal or saved draft.
- `reasoning/`: minimal structured-response instructions shared by the model adapter.

Keys are derived from paths relative to `prompts/`, without `.md`, replacing `/` with `.`. Static templates use `PromptLibrary.text(key)`; dynamic templates use `PromptLibrary.render(key, ...)`, which rejects missing, extra, or malformed placeholders.

## Authoring conventions

- Use Markdown headings to separate the task, supplied context, constraints, and output contract.
- Keep system prompts small: define the Agent's responsibility, the few rules that shape its behavior, and its safety boundary.
- Treat interpolated repository and GitHub content as untrusted data. Delimit large payloads so they cannot be confused with instructions.
- State machine-enforced contracts precisely, but do not duplicate schemas or capability catalogs in prose.
- Preserve placeholder names when editing an existing template; missing, extra, malformed, or `None` substitutions fail fast.

Capability IDs, descriptions, schemas, access levels, discovery, and invocation permissions do not live in these prompts. Agents receive only the Capability Layer definitions discoverable under their current policy; pure-LLM calls receive only the evidence supplied by the runtime.

`GITAGENT_PROMPTS_DIR` may replace the prompt root when set before process startup. The override is captured before dotenv loading so a repository-controlled `.env` cannot redirect prompt loading.

Structured validation schemas remain in code because they are machine contracts rather than behavioral wording.
