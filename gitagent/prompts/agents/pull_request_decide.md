Goal: {{goal}}
Entity: {{entity}}
{{guidance}}

Select exactly one Pull Request operation that matches the requested outcome:
- LIST, SEARCH, or SUMMARIZE for collections.
- GET for metadata/detail lookup that does not need Diff analysis.
- EXPLAIN for behavior, symbols, calls, and impact.
- REVIEW for implementation and test review.
- REVIEW_DIALOGUE for existing Reviews/comments, applicability, conflicts, or reply drafting.
- CI_ANALYZE for CI status or failure analysis without a code change.
- PLAN for a modification proposal without generating code.
- MODIFY for an explicit candidate code change to the PR.
- CI_FIX when CI analysis and an explicit candidate fix are both requested.
- POST_REVIEW only when the user explicitly asks to publish COMMENT, APPROVE, or REQUEST_CHANGES.
- MERGE_READINESS for readiness assessment without merging.
- MERGE only when the user explicitly asks to merge.

Set review_event only for POST_REVIEW. “Approve PR” means POST_REVIEW with APPROVE; it never means MERGE.
