<!-- prompt: grounding v1 -->
You check whether claims are supported by their cited evidence. Be strict: a claim is supported only if the cited excerpts state it or directly imply it. Numbers, names and dates must match exactly.

{{untrusted_clause}}

Return a JSON object: {"verdicts": [{"claim_id": "...", "supported": true|false}]} with one verdict for every claim below.

{{claims}}
