Operation: {{operation}}
Request: {{request}}

Produce a bounded repository retrieval plan.

- `queries` contains one to three concise literal code/content searches, ordered by value. For a conceptual request, expand it into likely APIs, identifiers, or terminology instead of repeating one generic word. Keep exact names when the user supplied them.
- `path_terms` contains zero to four filename or path fragments worth checking.
- `symbols` contains only identifiers that the user explicitly asks to locate, explain, or analyze as symbols. Do not classify a generic topic word as a symbol.
- Do not include GitHub qualifiers, prose explanations, duplicate values, or speculative queries unrelated to the request.
