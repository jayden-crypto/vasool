# Limitations

Written before anyone had to ask, and revised twice after adversarial reviews
found things this page had missed. Every number in the README should be read
against it.

## The comparison was unfair, and that is now fixed

The most load-bearing correction in this repository. The original rules baseline
had **no consent check and no contact-frequency cap** — 89% of its measured harm
came from two one-line checks it was never given. That made the kernel appear to
roughly double net value.

With the baseline corrected, **the kernel's net advantage is 1.01×.** What
survives is one row: 34 double-collect attempts against zero, because `I1`
requires a live provider read that no planner logic can substitute for.

The general lesson, recorded because it is the more useful finding: this project
was rigorous about provenance — seeds, sample sizes, which model produced which
number — and casual about whether the opponent had been given a fair chance.
Those are the two ways a benchmark misleads and only one was being watched.

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

## Amount conservation is per action, and not proven under concurrency

The property test establishes that **no single approved action collects more
than the order**. Cumulative conservation across several actions on one order
also holds, but through the case lifecycle rather than an invariant: a
successful collection closes the case, and I1 refuses anything further on a
settled order.

That is weaker than it sounds in one specific way. Two proposals gated
*simultaneously* against the same order could each read "unsettled" before
either executes, and I2's key is derived from the decision — so two genuinely
different decisions produce different keys and would not collide. Nothing in
this system runs concurrently, so the race is unreachable as written. But the
guarantee is a property of the runner's sequencing, not of the kernel, and a
production deployment with parallel workers would need a provider-side
reservation or a per-order lock before the claim holds.

Found by an adversarial review, not by me.

## The router result rests on 7 contradictory cases

The routing finding — 91.7% against a 93.3% ceiling — is measured on N=60, of
which only **7 are the contradictory cases the router exists to catch**. The
model got 6 of 7; the rules baseline got 0 of 7. That is a clean signal and a
thin one, and a re-run at N=500 would be the first thing to do before quoting
it anywhere load-bearing.

The router itself was designed **after** observing the per-style breakdown on
the test split. To limit the overfitting that invites, both escalation modes
were validated on the dev split first (rules 76.7% → routed 86.7%) and only then
measured on test. The routing rule reads no ground truth and is deterministic,
so it cannot be tuned per case — but the *choice* of rule was informed by
results, and that should be assumed rather than discovered.

## The economic case rests entirely on the harm prices

`make sensitivity` sweeps the harm multiplier over the committed 500-case
result. This is the sweep that should have existed from the start:

| harm × | B rules | E rules + kernel | E/B | winner |
|---:|---:|---:|---:|---|
| 2.00 | ₹3,31,204 | ₹3,55,363 | 1.07 | E |
| **1.00** | **₹3,95,904** | **₹4,00,663** | **1.01** | **E** |
| 0.50 | ₹4,28,254 | ₹4,23,313 | 0.99 | B |
| 0.25 | ₹4,44,429 | ₹4,34,638 | 0.98 | B |
| 0.00 | ₹4,60,604 | ₹4,45,963 | 0.97 | B |

**The kernel wins on net value only at roughly the prices I chose or above**, and
the margin at 1.0× is 1.2%. Halve them and the rules engine wins. This is a much
weaker economic claim than an earlier version of this repository made, and it is
the true one.

So the economic argument is close to a wash, and the case for the kernel should
not be made on it. The two arguments that do survive are not about money:

1. **I1 prevents a class of harm no planner can.** 34 double-collect attempts
   against zero. Consent and contact frequency are properties of state the
   planner holds, so a planner can enforce them — and once it does, those rows
   tie. Whether an order was paid moments ago through another channel is not,
   and no amount of planner logic substitutes for reading the provider at the
   moment of execution.
2. **Kernel checks bind any proposer; planner checks bind that planner.** Arms C
   and D are where this shows: the same model with no kernel produced 41
   double-collect attempts, 11 retries against risk declines and ₹574 actually
   double-charged. Behind the kernel, zero of each.

## Four of the six measured harms share code with the kernel

The README claims the environment measures harms independently of the arm. For
two of six that is true. For four it is not:

| Harm | Measured via | Independent? |
|---|---|---|
| `quiet_hours_violation` | `kernel.invariants.in_quiet_hours` | No — the same function I4 uses |
| `contact_to_opted_out` | `hidden.opted_out` | No — the same variable I5 reads |
| `futile_retry` | `raw_evidence.read` | No — the same function I6 uses |
| `risk_retry_strike` | `raw_evidence.read` | No — the same function I6 uses |
| `double_collect_attempt` | the environment's own settled state | **Yes** |
| `over_contacted` | hidden patience, never observable | **Yes** |

An arm scoring zero on the first four restates the kernel's own rules rather
than measuring anything. It shows the check was written, not that it survives a
world where truth differs from what you observe.

The example the README picked to demonstrate independence — noticing a 2am SMS —
is one of the dependent ones. The two genuinely independent harms are the two
that matter most, and `over_contacted` does not go to zero for the gated arm:
151 remain, which is the most honest number in this repository.

Fixing this properly means the environment deriving harm from hidden state
alone, never importing from `vasool.kernel`. It is the change I would make next.

## The router's contradictory-case result, both splits

`LIMITATIONS` previously cited the test split's 6/7 (85.7%) and the dev split's
*overall* figures, while omitting the dev split's contradictory figure — the one
the whole claim rests on:

| split | contradictory cases | model |
|---|---|---|
| test (reported in README) | 7 | 6/7 = 85.7% |
| dev (in the repo, previously uncited) | 10 | 6/10 = 60.0% |
| **combined** | **17** | **12/17 = 70.6%** |

Quoting the better of two available numbers on the metric the thesis depends on
is the kind of thing that is only visible to someone who reads both result
files. It is recorded here now. It is also moot, since the zero-model rule beats
both — but it was not moot when it was written.

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

## "Every arm faces the identical world" is true with three caveats

All three found by adversarial review, none of them fatal, none previously
written down.

**Common random numbers desynchronise almost immediately.** `uniform01` keys on
`case.attempts` — the arm's *own* counter, incremented only on executed actions.
The module docstring is precise about this ("at the same attempt index"); the
README dropped the qualifier. Since arm E executes 1,178 actions to arm B's
1,238, the pairing holds firmly for the first action on each case and degrades
after. There are also no confidence intervals anywhere, on a single seed.

**Out-of-band settlement is not exogenous.** It materialises only when the clock
advances, and the clock advances only when an arm acts. Against a configured 9%
(45 of 500), arms realise 19–21 — fewer than half ever fire. So "9% settle out
of band" describes the config, not the run. The direction is conservative: it
*understates* I1's value, since fewer double-collect opportunities arise than
configured.

**I4's quiet-hours clause never fires once.** Zero violations across every arm
on 500 cases — arm B because `next_sane_send_time` already avoids those hours,
arm C because it makes almost no contacts. One third of the flagship invariant
is completely unexercised by this benchmark, and the row was silently omitted
from the README's harm table rather than shown as a zero that means "untested"
rather than "prevented".

## Claims the code does not support, stated plainly

Each of these was written more strongly than the implementation warranted, and
found by adversarial review rather than by me.

**The ledger is corruption-evident, not tamper-evident.** Unkeyed SHA-256, no
signature, no external anchor. Anyone with write access can rewrite a record and
recompute the rest, and `verify()` has no independently-known tip to check
against. Needs an HMAC or an anchor; neither is built.

**Nothing reconstructs state from the ledger.** `Ledger.load` has one caller —
the trace viewer. There is no `resume` or `rebuild`. The ledger contains what a
recovery would need; the recovery is not written.

**The idempotency key is stable within a process, not across a restart.** It is
derived from `scheduled_for`, so a restarted process re-deriving the same
decision at a new instant computes a different key and I2 does not fire. Genuine
duplicate delivery of the same in-memory decision is caught; a restart is not.
The fix is a semantic identity — a decision sequence number — rather than a
timestamp.

**The MCP reconcile lookup cannot prove absence on a busy account.** The toolset
offers no filtered lookup, so it fetches a page and scans. It now raises rather
than reporting a false absence when the page is full, but on a large account it
will simply be unable to reconcile.

**One committed result file predates a fix.** `results/five-arm-local-n100.json`
reports `repairs_succeeded: 531` against `repairs_attempted: 46` — a counting
bug fixed in `llm.py` afterwards. Regenerating it means another 12-hour local
run, so the file stands with this note rather than being quietly refreshed.

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
