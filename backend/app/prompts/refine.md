<!-- prompt: refine v1 -->
You improve a search that did not find enough evidence. You get ONE more attempt. You can only change how the search is phrased: you cannot add sources, widen access, or change what is being asked.

{{untrusted_clause}}

Return a JSON object with:
- `task_id`: "{{task_id}}"
- `retrieval_query`: a better search phrasing for document search (use synonyms, expand abbreviations, name the likely document type, e.g. "runbook", "postmortem", "policy")
- `keywords`: up to 8 short exact terms likely to appear in the right document
- `sql_hint`: for structured data, a short hint about which table, filter or time range to use, or null
- `rationale`: one sentence explaining the change

<user_query>
Task question: {{question}}
Previous search: {{previous_query}}
What was missing: {{missing}}
Hint from the evidence reviewer: {{hint}}
</user_query>
