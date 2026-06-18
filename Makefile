.PHONY: setup verify test run scheduler

setup:
	python -m pip install -r requirements.txt

verify:
	bash scripts/verify.sh

test:
	python -m pytest -q

run:
	uvicorn app.main:app --reload

scheduler:
	python -m app.scheduler
