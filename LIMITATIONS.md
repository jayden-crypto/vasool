# Limitations

Written before anyone had to ask. Every number in the README should be read
against this page.

## The benchmark is a simulation

The 500-case batch is synthetic. The failure-class mixture, the issuer message
bank and the recovery physics are modelled on how Indian issuers and PSPs
behave, but they are a model, not a sample of production traffic.

**What that means for the numbers.** Relative comparisons between arms are the
finding — every arm faces the identical world, with common random numbers, so
the gaps measure architecture. The absolute recovery rate is a property of the
environment I wrote, and I would not quote it as a forecast for any real
merchant.

**What would settle it.** A retrospective replay against a real merchant's
failed-payment log, scoring each arm's chosen intervention against what
actually happened. That needs data I do not have.

The generator's parameters are in `config/generator.yaml`, committed and
seeded, so the world can be inspected rather than trusted.

## The live path is implemented but was not run

**This is the most important caveat on the page.** `vasool/executor/razorpay_rest.py`
and `vasool/executor/razorpay_mcp.py` are complete — real orders, a real
settlement read for I1, real payment links carrying the action key as
`reference_id` for idempotency, and a refusal to start on a non-test key. The
code paths are written and reviewed.

They were **not exercised against a real Razorpay account.** Razorpay's
onboarding gated test-key issuance behind a bank-account verification step, and
handing over bank details to unblock a demo was not a trade worth making. So
`make live` is untested against the live API, and every number in this
repository comes from the simulated environment.

What that means concretely: the backend interface is shared with the simulator
and exercised by the full test suite through it, so the Gate, the executor and
the arms are the same code either way — but the HTTP request shapes, the
`reference_id` duplicate-detection behaviour, and the order-status read have not
been confirmed against Razorpay's actual responses. Treat them as unverified.

Auto-retries (`RETRY_SAME_RAIL`, `RETRY_ALT_RAIL`) would not be exercisable live
in any case: replaying a stored instrument needs a saved token and a
recurring-enabled account, which a plain test key does not have. The live
backend raises rather than pretending.

## The kernel inherits the provider's mistakes

I6 re-derives futility from the raw error code, deliberately not from the
model's classification. That defends against a confident wrong diagnosis. It
does **not** defend against a confident wrong *issuer*: when a bank sends a
misleading reason code, the kernel is misled with it.

The benchmark generates such cases on purpose — 10% of events carry a reason
code that contradicts the issuer message — and they cost the system in both
directions. A wrong "dead instrument" code blocks a retry that would have
worked; a wrong benign code fails to block one that will not.

Fixing this properly means per-issuer reliability priors learned from outcomes,
which needs production volume.

## The contact budget is blunt

I4 allows three contacts per rolling week. The simulated customers have a
patience between one and four messages. So the kernel still over-contacts the
thin-patience tail — you can see it in the harm ledger, and it does not go to
zero.

A real system would learn a per-customer contact tolerance from response
history rather than applying one number to everyone. That is the single most
valuable thing I would build next.

## The stopping rule uses a fixed prior table

I7's expected-value calculation reads `config/priors.yaml`, which I wrote from
domain reasoning rather than fitted from data. Model confidence can only
attenuate those numbers, never raise them, so the failure mode is stopping too
early rather than looping forever — but a case that would have recovered on the
fourth attempt gets closed on the third.

## Diagnosis accuracy is measured on the first decision only

The reported accuracy compares the arm's first classification against hidden
ground truth. Later re-diagnoses within the same case are not scored, so the
number describes cold-start judgment, not how well an arm updates.

## The model arms measure a 7B, not language models

Arms C and D ran on `qwen2.5:7b` locally, because the hosted free tiers cap at
roughly 57 diagnoses per day (200,000 tokens, and a reasoning model spends
~3,500 per call) and local inference on the test machine runs at 82 seconds per
diagnosis. N=100 rather than 500 for the same reason.

**Do not read "the model scored 52%" as "language models score 52%."** A
frontier model may well clear the 81% rules baseline. The architecture is
unchanged either way — `VASOOL_PROVIDER` selects the provider and
`make bench-full` re-runs the identical comparison. Until someone runs it, the
supportable claim is narrow: on this task, this small model, behind this
prompt, lost to a good taxonomy.

What that does *not* qualify: the kernel results. Arm C versus arm D is a
comparison between two configurations of the same model, so the harm reduction
it demonstrates holds regardless of how good the model is.

## Which model produced which numbers

The reasoning zone is provider-agnostic, so the model arms can be run on a
frontier model, a hosted free tier, or a 7B on a laptop. **The results are not
interchangeable, and the results file records which provider produced them.**

A smaller model will classify worse. That is expected and it is worth stating
plainly rather than burying: if arm D on a local 7B beats the rules baseline,
the finding is that the architecture works with a cheap model. If it does not,
the finding is that this task needs a bigger one. Both are results; neither is
a reason to quietly swap the provider and reuse the old numbers.

Arms C and D issue one model call per decision. Responses cache by evidence
digest in `cache/llm_responses.json`, so a replay is free and byte-identical —
but the first run is not, and a fresh seed needs a fresh run.

## Not addressed at all

- **Multi-currency.** Everything assumes INR. I3 rejects anything else rather
  than converting.
- **Partial recovery accounting.** A part payment closes the case as recovered
  at the collected amount; the residual is not pursued.
- **Regulatory specifics.** Quiet hours and DND channel handling are modelled
  on the shape of Indian telecom rules, not written against the text of them.
  A production system needs a compliance review, not my reading.
- **Authentication of the customer reply channel.** The injection defence is
  structural — the model holds no authority — but the system does not verify
  that a reply came from the customer it claims to.
