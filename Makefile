PY := .venv/bin/python
LEDGER := $(shell ls -t runs/*-E-*.jsonl 2>/dev/null | head -1)

.PHONY: help setup test bench bench-dev bench-ledger bench-full faults trace live live-inject clean

help:
	@echo "Vasool — a recovery agent that is not allowed to move money"
	@echo
	@echo "  make setup        create the venv and install dependencies"
	@echo "  make test         kernel invariants, properties, faults, benchmark guarantees"
	@echo "  make bench        four-arm benchmark on the held-out split"
	@echo "  make bench-full   same, with model-backed arms (needs ANTHROPIC_API_KEY)"
	@echo "  make bench-dev    fast pass on the dev split, for iterating"
	@echo "  make faults       fault injection, including the prompt-injection demo"
	@echo "  make trace        render the most recent audit ledger"
	@echo "  make live         one real recovery against Razorpay test mode"
	@echo "  make live-inject  the same, with a prompt injection in the customer reply"

setup:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[llm,live,dev]"

test:
	$(PY) -m pytest vasool -q

bench:
	$(PY) -m vasool.bench.report --split test --arms A,B,E --ledger

bench-full:
	$(PY) -m vasool.bench.report --split test --arms A,B,C,D,E --ledger

bench-dev:
	$(PY) -m vasool.bench.report --split dev --n 120 --arms A,B,E

bench-ledger:
	$(PY) -m vasool.bench.report --split test --arms E --ledger

faults:
	$(PY) -m vasool.faults.demo

trace:
	@test -n "$(LEDGER)" || (echo "no ledger yet — run 'make bench' first" && exit 1)
	$(PY) -m vasool.cli.trace $(LEDGER)

live:
	$(PY) -m vasool.cli.live

live-inject:
	$(PY) -m vasool.cli.live --inject

clean:
	rm -rf runs/ .pytest_cache/ && find . -name __pycache__ -type d -exec rm -rf {} +
