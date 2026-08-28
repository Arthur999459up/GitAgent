# Select a Pull Request Operation

## Request

- **Goal:** {{goal}}
- **Selected entity:** {{entity}}{{guidance}}

## Operation map

Choose exactly one operation that matches the user's requested outcome:

| Operation | Use when |
| --- | --- |
| `LIST`, `SEARCH`, `SUMMARIZE` | The request concerns a PR collection. |
| `GET` | The user wants PR metadata or details without Diff analysis. |
| `EXPLAIN` | The user asks about changed behavior, symbols, call relationships, or impact. |
| `REVIEW` | The user asks for implementation or test review. |
| `REVIEW_DIALOGUE` | The request concerns existing Reviews/comments, current applicability, conflicts, or a reply draft. |
| `CI_ANALYZE` | The user asks for CI status or failure analysis without requesting a code change. |
| `PLAN` | The user wants a modification plan without candidate code. |
| `MODIFY` | The user explicitly requests a candidate code change to the PR. |
| `CI_FIX` | The user explicitly requests both CI analysis and a candidate fix. |
| `POST_REVIEW` | The user explicitly asks to publish a `COMMENT`, `APPROVE`, or `REQUEST_CHANGES` Review. |
| `MERGE_READINESS` | The user asks whether the PR is ready to merge, without asking to merge it. |
| `MERGE` | The user explicitly asks to merge the PR. |

Set `review_event` only for `POST_REVIEW`; otherwise set it to an empty string. “Approve this PR” means `POST_REVIEW` with `APPROVE`, never `MERGE`.
