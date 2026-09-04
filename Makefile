# Prefer the project venv, fall back to whatever python is on PATH so the
# targets work on a fresh clone and on Windows.
PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
LEDGER := $(shell ls -t runs/*-E-*.jsonl 2>/dev/null | head -1)

.PHONY: help setup .check-deps test bench bench-dev bench-ledger bench-full bench-local bench-local-quick warm warm-local classify ceiling sensitivity replicate faults trace live live-inject clean

help:
	@echo "Vasool — a recovery agent that is not allowed to move money"
	@echo
	@echo "  make setup        create the venv and install dependencies"
	@echo "  make test         kernel invariants, properties, faults, benchmark guarantees"
	@echo "  make bench        four-arm benchmark on the held-out split"
	@echo "  make bench-full   same, with model-backed arms (needs a provider)"
	@echo "  make bench-local  model arms on a local Ollama model, at zero cost"
	@echo "  make warm-local   pre-compute diagnoses in parallel into the cache"
	@echo "  make bench-dev    fast pass on the dev split, for iterating"
	@echo "  make classify     rules vs model vs router, one call per case"
	@echo "  make replicate    twelve independent worlds, with confidence intervals"
	@echo "  make sensitivity  how much of the result is the harm-price model"
	@echo "  make ceiling      how much of the batch is decidable at all"
	@echo "  make faults       fault injection, including the prompt-injection demo"
	@echo "  make trace        render the most recent audit ledger"
	@echo "  make live         one real recovery against Razorpay test mode"
	@echo "  make live-inject  the same, with a prompt injection in the customer reply"

setup:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[llm,live,dev]"

# Every target that needs the project's dependencies goes through this, so a
# reviewer who runs `make test` before `make setup` gets a sentence rather than
# a traceback.
.check-deps:
	@$(PY) -c "import pytest, pydantic, yaml, rich" 2>/dev/null || { \
	  echo ""; \
	  echo "  Dependencies are not installed. Run:"; \
	  echo ""; \
	  echo "      make setup"; \
	  echo ""; \
	  echo "  (needs uv: https://docs.astral.sh/uv/ — or pip install -e '.[dev]')"; \
	  echo ""; \
	  exit 1; }

test: .check-deps
	$(PY) -m pytest vasool -q

bench: .check-deps
	$(PY) -m vasool.bench.report --split test --arms A,B,E --ledger

bench-full: .check-deps
	$(PY) -m vasool.bench.report --split test --arms A,B,C,D,E --ledger

# Model arms on a local model, at zero cost. Needs `ollama serve` running.
bench-local: .check-deps
	VASOOL_PROVIDER=ollama VASOOL_MODEL=$${VASOOL_MODEL:-qwen2.5:7b} \
	$(PY) -m vasool.bench.report --split test --arms A,B,C,D,E --ledger

# Compute every first diagnosis in parallel and cache it. On a laptop-hosted
# model this is most of the wall clock, and it is embarrassingly parallel.
warm:
	$(PY) -m vasool.diagnosis.warm --split test --workers $${WORKERS:-4}

warm-local:
	VASOOL_PROVIDER=ollama VASOOL_MODEL=$${VASOOL_MODEL:-qwen2.5:7b} \
	$(PY) -m vasool.diagnosis.warm --split test --workers $${WORKERS:-3}

# Fast sanity pass on the model arms before spending a full run.
bench-local-quick:
	VASOOL_PROVIDER=ollama VASOOL_MODEL=$${VASOOL_MODEL:-qwen2.5:7b} \
	$(PY) -m vasool.bench.report --split dev --n 40 --arms B,C,D,E

bench-dev: .check-deps
	$(PY) -m vasool.bench.report --split dev --n 120 --arms A,B,E

bench-ledger: .check-deps
	$(PY) -m vasool.bench.report --split test --arms E --ledger

# One model call per case instead of ~5.4 — isolates the classification claim.
#
# Warns rather than silently degrading: the model name below is a hosted one, so
# running this while .env points VASOOL_PROVIDER at ollama asks Ollama for a
# model it does not have and every call fails into the rules fallback — a table
# of zeros that looks like a result.
classify: .check-deps
	@$(PY) -c "import os,sys; sys.path.insert(0,'.'); \
	  from vasool.core import env; env.load(); \
	  p=os.environ.get('VASOOL_PROVIDER',''); m='$${VASOOL_MODEL:-qwen/qwen3.8-27b}'; \
	  sys.exit(0) if (p!='ollama' or '/' not in m) else \
	  (print('\n  VASOOL_PROVIDER=ollama but the model is \''+m+'\', which is a hosted id.'), \
	   print('  Set VASOOL_PROVIDER=openai_compat in .env, or pass a local model:'), \
	   print('\n      VASOOL_MODEL=qwen2.5:7b make classify\n'), sys.exit(1))"
	$(PY) -m vasool.bench.classify --n $${N:-60} --split test \
	  --models "$${VASOOL_MODEL:-qwen/qwen3.8-27b}" \
	  --routed "$${VASOOL_MODEL:-qwen/qwen3.8-27b}" --mode conflict_only

replicate: .check-deps
	$(PY) -m vasool.bench.replicate --reps $${REPS:-12} --n $${N:-300}

sensitivity: .check-deps
	$(PY) -m vasool.bench.sensitivity

ceiling: .check-deps
	$(PY) -m vasool.bench.ceiling --split test

faults: .check-deps
	$(PY) -m vasool.faults.demo

trace: .check-deps
	@test -n "$(LEDGER)" || (echo "no ledger yet — run 'make bench' first" && exit 1)
	$(PY) -m vasool.cli.trace $(LEDGER)

live:
	$(PY) -m vasool.cli.live

live-inject:
	$(PY) -m vasool.cli.live --inject

clean:
	rm -rf runs/ .pytest_cache/ && find . -name __pycache__ -type d -exec rm -rf {} +
