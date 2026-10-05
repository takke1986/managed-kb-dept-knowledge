# Implementation plan: KB ingestion audit

- [ ] 1. Set up the audit package and test tooling
  - Create `audit/__init__.py` and `tests/` with a `conftest.py` that puts the repository
    root and `functions/` on `sys.path`
  - Add `pytest` and `hypothesis` to the `dev` dependency group in `pyproject.toml`
  - _Requirements: 4_

- [ ] 2. Implement normalisation
  - [ ] 2.1 Write `audit/normalise.py` with `normalise(text)` (NFKC, then remove whitespace)
    - _Requirements: 1.1, 1.3_
  - [ ] 2.2 Unit tests: full-width digits and letters, ideographic space, line breaks
    - _Requirements: 1.1, 1.3_
  - [ ] 2.3 Property tests for properties 1-3 (idempotence, whitespace, width)
    - _Requirements: 1.1, 1.2, 1.3_

- [ ] 3. Implement the verdict
  - [ ] 3.1 Write `audit/matching.py` with `Verdict`, `Result` and `classify`
    - Check order: image metadata, empty, missing source, substring
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [ ] 3.2 Unit tests from measured cases: `膳所営業所` -> `陸所営業所`, `○%` -> `0%`,
    image chunk, empty chunk, missing source
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [ ] 3.3 Property tests for properties 4-6
    - _Requirements: 2.1, 2.2, 2.3_

- [ ] 4. Implement span reporting
  - [ ] 4.1 Write `audit/spans.py` with `Span` and `diff_spans`
    - _Requirements: 3.1, 3.2, 3.3_
  - [ ] 4.2 Unit test: a substituted proper noun is reported as exactly that pair
    - _Requirements: 3.2_
  - [ ] 4.3 Property tests for properties 7-8
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 5. Implement collection
  - [ ] 5.1 Write `audit/collect.py` with `source_key_for` (reuse `DocRef`) and
    `collect_chunks` (de-duplicate on location plus text)
    - _Requirements: 4.1, 4.2, 5.1_
  - [ ] 5.2 Unit tests with a fake `retrieve`: duplicates across queries, a chunk from
    another department
    - _Requirements: 4.2, 5.2_
  - [ ] 5.3 Property test for property 9
    - _Requirements: 4.2_

- [ ] 6. Wire the command line
  - [ ] 6.1 Write `scripts/audit_ingestion.py`: validate the department against
    `config/app.json` before any AWS call, sign in as a member, call `kb___Retrieve` on
    the Gateway per query with the department filter, read sources with `GetObject`
    - _Requirements: 4.1, 4.6, 5.1, 5.3_
  - [ ] 6.2 Print the summary, write `--json`, set the exit status; never print the text of
    an isolation failure
    - _Requirements: 4.3, 4.4, 4.5, 5.2_
  - [ ] 6.3 Unit test the CLI with faked AWS calls: unknown department exits 2 without AWS
    calls; one altered chunk exits 1
    - _Requirements: 4.5, 5.3_

- [ ] 7. Verify against the deployed stack
  - Run `uv run python scripts/audit_ingestion.py <department>` for each department in
    `config/app.json` and record the summary in `README.md`
  - _Requirements: 4.3, 4.5_
