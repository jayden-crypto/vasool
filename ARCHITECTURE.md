# Architecture

## The thesis

> Language models are excellent at diagnosis over messy evidence and unfit to
> hold authority. So the model reasons without power, and the kernel holds power
> without reasoning.

Everything below is a consequence of that sentence.

The usual agentic build inverts it: give the model the tools, write careful
instructions about what not to do, and hope. That design has no failure mode
between "works" and "moved money it should not have", because the component
being instructed is the same component that can be argued with.

---

## The authority boundary

```
     REASONING ZONE                    │              AUTHORITY ZONE
     no credentials                    │              holds the keys
     unbounded inference               │              zero inference
                                       │
  evidence bundle                      │        ┌──────────────────────┐
   payment · error · issuer prose      │        │  The Gate            │
   customer history · merchant config  │        │  I1 … I7 in order    │
        │                              │        │  collects ALL denials│
        ▼                              │        └──────────┬───────────┘
  diagnosis                            │                   │ allowed
   closed taxonomy · confidence        │                   ▼
   evidence citations                  │        ledger.append("intent")  ◄── I8
        │                              │                   │ receipt
        ▼                              │                   ▼
  proposal                             │        backend.execute(key)
   intervention · channel · amount     │           REST or MCP, idempotent
   timing · rationale                  │                   │
        │                              │                   ▼
        └──────────────────────────────┼──────► ledger.append("outcome")
                                       │
                              AUTHORITY BOUNDARY
```

**What crosses the boundary:** one JSON object matching
`vasool/diagnosis/schema.py::Proposal`. Nothing else. No tool call, no URL, no
key, no free-form command.

**What comes back:** nothing. The reasoning zone gets a repair note on denial —
the reason codes and a detail string — and one bounded attempt to propose
something else. It never learns whether the action succeeded, because it has no
use for that within a decision.

### Why the schema is closed

`FailureClass` and `Intervention` are enums, and the model's output is validated
against them. A model cannot name a failure class no policy covers or an
intervention the executor has no code path for. When it tries — see the
`out_of_taxonomy` fault scenario — validation fails, and the deterministic path
takes the decision with the case tagged `degraded`.

This is cheaper than it looks. Constraining the output space does not constrain
the reasoning; it constrains what the reasoning is allowed to *mean* on the
other side.

### The one thing the adapter deliberately does not do

`LLMDiagnoser._to_action` carries the model's proposed amount across
**verbatim**, including when it is absurd. A proposal to collect ₹50,000 on a
₹2,724 order is passed to the Gate unmodified.

Clamping it there would be the obvious "safe" move and it would be wrong. It
moves the defence into the component that can be talked into things, and it
makes the injection test pass for the wrong reason — you would be testing a
sanitiser, not an invariant. The number has to reach I3 to prove I3 stops it.

---

## The kernel

Eight invariants. Seven are pure functions over a `GateContext`; the eighth
governs ordering and lives at the executor.

### What the kernel bounds

`GATED_INTERVENTIONS`, not `MONEY_MOVING`. The distinction was a real bug: an
adversarial review found that `HANDOFF_HUMAN` — which moves no money and so sat
outside `MONEY_MOVING` — skipped I1, I2 and I7's caps entirely, while being the
priciest action in `costs.yaml` at ₹50, a hundred times a payment link.

The demonstrated consequence was a human being paged to chase a customer who had
already paid: precisely the harm I1 exists to prevent, arriving through the one
door I1 was not watching. In receivables that is the headline compliance
failure, not an edge case.

*"Moves money"* was the wrong predicate. *"Consumes something we cannot get
back"* is the right one, and there is a test asserting every intervention priced
at or above ₹50 is inside the gated set — so adding an expensive action later
cannot silently reintroduce the hole.

### I1 — `no_double_collect`

**Prevents:** collecting money that already arrived.

The shape of the bug in production: a customer sees the failure and pays through
another route while a recovery workflow is mid-flight. The workflow's own state
says nothing has been collected, because nothing *it* did collected anything.

So I1 does not read case state. It reads live provider state, through a
`SettlementReader` the Gate is constructed with —
`GET /v1/orders/{id}` on the REST backend, `fetch_order` on the MCP backend.
Stale local belief is exactly the thing being defended against, so consulting it
would defeat the invariant.

**Unknown is not unsettled.** If the provider cannot be read, `is_settled`
raises rather than returning False, and the Gate denies every money-moving
action with `SETTLEMENT_UNKNOWN`. An earlier version returned False on a read
failure with a comment claiming that was the safe direction; it was the
opposite, since False means "not settled" and lets I1 approve. An adversarial
review found it. Actions that move no money are unaffected — a WAIT does not
need a reachable provider.

**What it costs:** one API read per money action, and an outage stops recovery
rather than risking a double charge. That is the correct trade for this
invariant and it should be a deliberate one.

**Measured:** ungated arms register 34–36 double-collect attempts across 500
cases; the gated arm registers zero and closes 19 cases as *settled elsewhere,
no credit taken*.

This is the invariant that survives a fair comparison. Once the rules baseline
is given consent and contact-frequency checks — which it should always have had
— every other harm row ties, and net value comes to 1.01×. I1 does not tie,
because it is the only check that cannot be written as planner logic: whether an
order was paid moments ago through another channel is not a property of state
the planner holds. It requires reading the provider at the moment of execution.

### I2 — `idempotent_write`

**Prevents:** one decision becoming two charges.

The key is `sha256(case_id : intervention : amount : currency : scheduled_for)`.

**This was wrong in the first version and the fault suite caught it.** The key
originally included `case.attempts`, a counter that advances after execution — so
a replayed intent computed a *different* key and sailed straight past the
duplicate check. The duplicate-delivery scenario failed, which is what a fault
suite is for. The key is now a function of the decision itself: two deliveries
of one decision collide; two genuinely different decisions do not. There is a
regression test named for it.

### I3 — `amount_conserving`

**Prevents:** anything at all changing what gets charged.

Three rules: currency must match the order and be permitted; the amount may
never exceed the order under any intervention; and it must equal the order
exactly, unless the intervention is `PART_PAYMENT_LINK` and both policy and
merchant allow it, in which case it must clear the higher of the two floors.

This is the invariant that makes prompt injection a non-event. The property test
in `test_properties.py` states it precisely: over arbitrary proposals — any
intervention, any channel, amounts up to a billion paise, any currency, any case
history — **no single action the Gate approves ever collects more than the
order.** That holds regardless of what the model was persuaded to ask for, which
is why the defence is structural rather than a filter chasing new phrasings.

**Be exact about the scope**, because the stronger version is tempting and is
not what is tested. The property is *per action*. Cumulative conservation across
several actions on one order holds in this system too, but by a different
mechanism: a successful collection closes the case (`CaseStatus.RECOVERED`), and
I1 refuses anything further on a settled order. That is a property of the case
lifecycle rather than of an invariant, and it is **not established under
concurrency** — two proposals gated simultaneously against the same order could
each read "unsettled" before either executes. Nothing in this system runs
concurrently, so the race is unreachable today; it is an unproven guarantee
rather than a live defect, and closing it properly means a provider-side
reservation, not another invariant.

### I4 — `contact_budget`

**Prevents:** a sequence of individually reasonable messages that is collectively
harassment.

Rolling window cap, per-channel cooling-off, quiet hours computed in the
customer's local time, channel allow-list. Enforced in code because an
instruction to "be considerate" in a prompt is not a rate limit.

**What it costs, honestly:** it is blunt. The policy allows three contacts a
week; simulated customers tolerate between one and four. The gated arm still
over-contacts 151 times across 500 cases — down from 605, but not zero. A per-
customer tolerance learned from response history is the right fix and needs
production data. This is in [LIMITATIONS.md](LIMITATIONS.md).

### I5 — `consent_honored`

**Prevents:** routing around someone who asked to be left alone.

Opt-out is terminal for every channel. DND blocks the channels it legally covers
(SMS, voice) and not the ones it does not — a distinction worth making, because
collapsing it either over-blocks legitimate email or under-blocks SMS.

**Measured:** zero in both arms, now that the baseline has a consent check. The
earlier figure of 65 measured a baseline that had none, which flattered the
kernel and was corrected after an adversarial review. The kernel's remaining
advantage here is architectural rather than numerical: I5 binds every proposal
from any source, where a planner check binds only that planner. Arms C and D
measure the difference.

### I6 — `no_futile_retry`

**Prevents:** replaying instruments that cannot work, and retrying risk declines,
which is worse than useless — it degrades the merchant's own decline rate.

The important detail is the source of truth. `vasool/kernel/raw_evidence.py`
reads the provider's error code directly and answers three questions itself: is
the instrument provably dead, is the mandate provably dead, was this a risk
decline. **The model's classification is not consulted.**

So a confident wrong diagnosis cannot unlock a futile retry. There is a test
named `test_i6_ignores_a_confident_diagnosis_that_contradicts_the_evidence`, and
a fault scenario that runs a model asserting `ISSUER_DOWN` at confidence 1.0 over
a `card_expired` error. Denied.

The mapping is deliberately conservative — a code appears in `raw_evidence` only
when it admits exactly one reading. Ambiguity is left to the diagnosis layer,
where being wrong is cheap.

**Measured:** 220 futile retries and 27 risk-decline retries in the cron arm.
Zero in any gated arm.

### I7 — `stopping_rule`

**Prevents:** the unbounded loop.

Three independent guarantees, so termination does not depend on the economics
being well calibrated:

1. **Horizon.** Past `horizon_days` from case open, nothing runs.
2. **Attempt cap.** `max_money_actions` per case.
3. **Expected value floor.** Stop when `p × amount < cost × min_ev_to_cost_ratio`,
   or when `p` falls below an absolute floor.

`p` comes from `config/priors.yaml`, committed so the stopping rule is auditable
rather than a hidden knob, and decayed by attempt count. **A model's confidence
can only attenuate the prior, never raise it** — `success_prior` multiplies by
`min(1.0, confidence)` when the source is `llm` and leaves it alone otherwise, so
an overconfident diagnosis cannot buy itself more budget. There is a test.

### I8 — `audit_before_action`

**Prevents:** the gap between deciding and acting.

The executor writes the intent record, receives a digest, checks it, and only
then calls the backend. A crash between the two leaves a chain that says exactly
how far we got — the `crash_mid_batch` scenario reconstructs the executed
idempotency keys from the ledger alone and asserts they match in-memory state.

The ledger is append-only JSONL where each record commits to the previous
digest. `verify()` recomputes the chain; the demo tampers with a payload and
watches it fail at the exact sequence number.

---

## The unknown-outcome problem

The hardest thing in this codebase is four lines long.

A write to Razorpay times out. The action may have taken effect. A timeout on a
write **is not a decline**, and the naive response — retry — charges the customer
twice.

`Executor._resolve_unknown` does this instead:

1. Log `unknown_outcome` to the ledger with the idempotency key.
2. Ask the backend to `reconcile(key)` — for REST, `GET /v1/payment_links?reference_id=<key>`.
3. If the action landed, record it as the outcome. Never replay.
4. If it provably did not land, replay with the **same** key, so a second timeout
   cannot produce a second action either.
5. If the replay also fails, give up cleanly and leave the case open.

**And the case behind that case.** Step 2 is itself a read against a provider we
reached this code path because we could not reach. An adversarial review found
that its failure was being swallowed — `_find_by_reference` returned `None`,
which step 3 reads as *provably absent*, which licenses the replay in step 4 and
writes `action_absent` into a tamper-evident ledger as though it were a fact.

It now raises `ReconciliationUnknown`, the executor refuses to replay on an
unproven absence, and the ledger records `reconcile_failed_outcome_unknown`.
Fault scenario 11 injects exactly this, and asserts the provider sees one write
rather than two.

The `TimingOutBackend` fault wrapper models the honest version of this: it times
out *and still applies the write*. A system that assumed failure would double
charge. The scenario asserts the collected amount is exactly the order amount or
zero — never twice.

---

## Where the model was supposed to earn its place, and did not

The rules baseline (`vasool/diagnosis/fallback.py`) classifies at **86.6%** on
the held-out split. It is the same code as the degraded path, which is
deliberate: the fallback has to be good, because the fallback is what runs when
the model is unavailable.

It is not a strawman. It has a reason-code table, a real keyword table over
issuer prose, an attempt cap, escalation on repeat, and send-time scheduling that
avoids unsociable hours on its own. Making the baseline stronger is the honest
direction to tune in — every point it gains is a point the model has to earn.

It fails in one specific, interesting way. The benchmark generates three evidence
styles: 55% clean reason codes, 35% generic `payment_failed` with the cause only
in issuer prose, and **10% where the reason code contradicts the message**. A
lookup table believes the code. Weighing a machine code against free text that
disagrees with it is the thing a language model is supposed to be good at, and
it was the entire justification for putting one in the architecture.

**It did not deliver.** `qwen2.5:7b` scored **52.0%** against the baseline's
81.0% on the N=100 run — 29 points *below* the lookup table, on a batch whose
ceiling is 98.2%. The benchmark was built to be able to say that, and it said it.

Two things follow for the architecture, and they point in opposite directions.

The boundary is vindicated by the model failing rather than despite it. Arm D
recorded zero harms while carrying a diagnoser that was wrong half the time;
arm C, the same model with no kernel, produced 41 double-collect attempts, 11
retries against risk declines and ₹574 actually double-charged. `I6` alone
refused 934 futile retries the model proposed, deciding from raw evidence rather
than the model's classification — the exact scenario that invariant was written
for, arriving unprompted.

The reasoning zone is not vindicated. On this task, at this model size, the
deterministic taxonomy is simply better, and arm E — rules behind the same
kernel — wins on net value by more than 2x. The right thing to ship today is
arm E. The model arm stays in the repository because the comparison is the
finding, and because `VASOOL_PROVIDER` makes re-running it against a stronger
model a configuration change rather than a rewrite.

This is worth stating plainly rather than softening: as originally framed, the
hypothesis is unsupported by its own measurement. A model asked to diagnose
every case is worse than a lookup table.

### The router, and why its model call was removed

Isolating classification (`vasool/bench/classify.py`, one call per case instead
of ~5.4) made a stronger model affordable. On the held-out split with
`qwen/qwen3.8-27b`:

| Diagnoser | Overall | clean code | prose only | contradictory | calls |
|---|---:|---:|---:|---:|---:|
| rules | 81.7% | 100.0% | 86.2% | **0.0%** | 0 |
| model, raw | 80.0% | 83.3% | 75.9% | 85.7% | 60 |
| model, routed | 91.7% | 100.0% | 86.2% | 85.7% | 8 |
| **rules + believe-the-prose** | **91.7%** | 95.8% | 86.2% | **100.0%** | **0** |

The two diagnosers fail in complementary ways and the aggregate hid it. Rules
are perfect where the code states the cause and score zero where the code
contradicts the prose. So `decide_route` asks whether the two sources agree, and
escalates only on disagreement.

**Then the escalation turned out to be unnecessary.** Detecting a disagreement
requires computing a prose classification, so the router is holding a candidate
answer at the moment it decides to ask a model for one. `classify_adjudicated`
replaces the call with a single rule — on conflict, believe the prose — and
matches the routed model at N=60, then beats it at N=500 (95.4% against 86.6%
for the plain baseline, on a 98.2% ceiling) with no model at all.

The cause is structural, not statistical. `_build_error` builds a contradictory
case by keeping the true class's issuer message and swapping in a random wrong
code. The prose is truthful by construction on every such case, which makes
"believe the prose" the generator's own answer key. No model, however large,
recovers value the rule has not already taken.

What this leaves standing, precisely:

* **The routing decision earns its place.** Knowing *which* cases are ambiguous
  is genuinely useful and is computed deterministically from evidence.
* **The escalation target does not.** On this benchmark there is nothing worth
  escalating to.
* **The claim a model adjudicates conflicting evidence better is unsupported**
  by the data in this repository, and this benchmark cannot support it, because
  it never emits a case where the code is right and the prose is wrong. Building
  that generator is the next experiment.

The router remains in the codebase with the model path intact, because the
comparison is the finding and because the world where it wins is a config change
away rather than a rewrite.
---

## Benchmark methodology

**Common random numbers.** Every stochastic draw is
`uniform01(seed, case_id, attempt_index, action)`. Two arms taking the same
action on the same case at the same attempt index get the same roll. The
comparison is paired, which removes the variance that would otherwise swamp a
500-case difference.

**Hidden state is structurally hidden.** `HiddenState` lives in the environment
and never appears on `FailureEvent`. There is a test asserting the observable
event carries no field that leaks the answer.

**The environment measures harms independently.** It notices a 2am SMS, a contact
to an opted-out customer, or a collection on a settled order whether or not the
arm that caused it has any concept of those things. A self-reported harm ledger
would be worthless.

**Nothing is credited that was not earned.** Out-of-band settlements go to the
customer, not to an arm. A collection on an already-settled order is counted as a
double charge, subtracted from net value, and never as a recovery.

**Dev and test are different worlds.** Iteration happened on the `dev` seed.
Everything reported is `test`.

---

## What I got wrong

**The idempotency key.** Derived from a mutable counter, so replays computed
different keys. Found by my own duplicate-delivery scenario, not by reasoning.
The lesson generalises: an idempotency key must be a function of the decision,
never of the state that the decision advances.

**The rules baseline looped.** The first version had no attempt cap, so it
re-proposed a payment link until the horizon — 9.4 contacts per case — and the
kernel looked spectacular against it. That is a rigged comparison and a reviewer
would have seen through it in seconds. It now has an attempt cap and send-time
scheduling, which cost the kernel most of its apparent margin on contact harms
and left the result honest.

**Case accounting did not close.** Cases resolved as out-of-band settlements fell
into no bucket, so 499 of 500 cases were accounted for and the discrepancy looked
like a non-terminating case. There is now a `closed_cases` property and a test
that every case reaches exactly one terminal state.

**The first stopping rule had no floor on probability**, only the EV ratio, so
large-amount cases stayed workable at absurdly low success odds. Added
`min_success_probability` as an independent guard.
