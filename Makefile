# Standard-library Python; no install step. Leaks fail the build.
.PHONY: test

test:   ## run the suite (raw-SQL tests prove the rules live in the database)
	python3 -W error::ResourceWarning -m unittest discover -s tests -v
