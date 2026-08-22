# Response cache

Model responses, keyed by `sha256(evidence_digest | prompt_version | model | effort | repair_round)`.

Committed on purpose. It is what lets someone without an `ANTHROPIC_API_KEY`
replay a benchmark run byte-for-byte instead of taking the numbers on trust.
Changing the prompt version or the model id produces a clean miss rather than a
silently stale result.
