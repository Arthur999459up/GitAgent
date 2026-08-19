# gitagent/prompts — LLM-facing prompts

Prompt wording lives in this directory so behavior text can be tuned without changing runtime code. `PromptLibrary` loads the Markdown templates once at startup and performs strict `{{placeholder}}` substitution for dynamic templates.

## Directories

- `system/`: one concise system prompt per Agent.
- `routing/`: routing and approval-classification input templates.
- `agents/`: task-specific evidence/output templates.
- `reasoning/`: structured-output response contracts.

Keys are derived from paths relative to `prompts/`, without `.md`, replacing `/` with `.`. Static templates use `PromptLibrary.text(key)`; dynamic templates use `PromptLibrary.render(key, ...)`, which rejects missing, extra, or malformed placeholders.

Tool names, descriptions, input schemas, output schemas, and access levels do not live in prompts. Tool-enabled Agents receive their allowed MCP function definitions from the Tool Registry; pure-LLM calls receive only supplied evidence.

`GITAGENT_PROMPTS_DIR` may replace the prompt root when set before process startup. The override is captured before dotenv loading so a repository-controlled `.env` cannot redirect prompt loading.

Structured validation schemas such as routing/approval contracts remain in code because they are machine contracts rather than prompt wording.
