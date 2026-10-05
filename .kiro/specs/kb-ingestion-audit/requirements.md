# Requirements: KB ingestion audit

## Introduction

Amazon Bedrock Managed Knowledge Bases re-parse PDF, Word, PowerPoint, CSV and Excel
documents some minutes after ingestion, and the second result replaces the first. In
Japanese this corrupts text without any error: proper nouns become other names, clauses
disappear, blanks become numbers. This project avoids it by sending only Markdown to the
KB (see `.kiro/steering/kb-ingestion.md`), but nothing currently proves the rule held.

The ingestion audit retrieves what the knowledge base actually indexed, compares every
chunk with the Markdown we sent, and reports any chunk that is not a verbatim excerpt.
It turns "we believe the KB has our text" into a check that can be run and repeated.

## Glossary

- **Source**: the Markdown file under `kb-source/<department>/<doc_id>/` that we sent to the KB.
- **Chunk**: one retrieval result returned by the KB, with its text and metadata.
- **Normalised text**: text after Unicode NFKC and removal of all whitespace.
- **Verbatim**: a chunk is verbatim when its normalised text is a substring of the
  normalised text of its source.
- **Image chunk**: a chunk whose metadata contains `_media_type: image`.

## Requirements

### Requirement 1: Normalisation

**User Story:** As an operator, I want layout differences ignored, so that only real
changes to the text are reported.

#### Acceptance Criteria

1. WHEN text is normalised THE SYSTEM SHALL apply Unicode NFKC and then remove every
   whitespace character.
2. WHEN normalised text is normalised again THE SYSTEM SHALL return it unchanged.
3. WHEN two texts differ only in whitespace or in full-width versus half-width forms
   THE SYSTEM SHALL produce identical normalised text for both.

### Requirement 2: Deciding whether a chunk is verbatim

**User Story:** As an operator, I want a strict yes/no verdict per chunk, so that a single
changed number is never hidden by a high similarity score.

#### Acceptance Criteria

1. WHEN the normalised chunk text is a substring of the normalised source text THE SYSTEM
   SHALL mark the chunk as verbatim.
2. WHEN the normalised chunk text is not a substring of the normalised source text THE
   SYSTEM SHALL mark the chunk as altered, regardless of how similar the two texts are.
3. WHEN a chunk's metadata contains `_media_type: image` THE SYSTEM SHALL mark the chunk as
   skipped and SHALL NOT count it as verbatim or altered.
4. IF the source of a chunk cannot be identified or read THEN THE SYSTEM SHALL mark the
   chunk as unmatched and SHALL report the reason.
5. WHEN a chunk's normalised text is empty THE SYSTEM SHALL mark it as skipped.

### Requirement 3: Showing what changed

**User Story:** As a reviewer, I want to see only the part that changed, so that I can
judge it in seconds instead of reading the whole chunk.

#### Acceptance Criteria

1. WHEN a chunk is altered THE SYSTEM SHALL locate the region of the source that best
   matches the chunk.
2. WHEN a chunk is altered THE SYSTEM SHALL report each differing span as a pair of
   source text and chunk text, each no longer than 40 characters.
3. WHEN reporting spans THE SYSTEM SHALL report at most 5 spans per chunk.

### Requirement 4: Running the audit

**User Story:** As an operator, I want to audit one department's knowledge base view from
the command line, so that I can check it after every ingestion.

#### Acceptance Criteria

1. WHEN the operator runs the audit for a department THE SYSTEM SHALL retrieve chunks
   using the department's metadata filter and a configurable list of queries.
2. WHEN the same chunk is returned by more than one query THE SYSTEM SHALL audit it once.
3. WHEN the audit finishes THE SYSTEM SHALL print a summary with the number of chunks
   audited, verbatim, altered, skipped and unmatched.
4. WHEN the operator passes `--json <path>` THE SYSTEM SHALL write every per-chunk result
   to that file.
5. WHEN at least one chunk is altered or unmatched THE SYSTEM SHALL exit with status 1;
   otherwise THE SYSTEM SHALL exit with status 0.
6. THE SYSTEM SHALL only read from AWS (Retrieve and S3 GetObject) and SHALL NOT create,
   modify or delete any AWS resource.

### Requirement 5: Department isolation

**User Story:** As an administrator, I want the audit to respect department boundaries,
so that running it never exposes one department's text to another.

#### Acceptance Criteria

1. WHEN auditing a department THE SYSTEM SHALL compare chunks only with sources under that
   department's `kb-source/<department>/` prefix.
2. IF a retrieved chunk's source is under another department's prefix THEN THE SYSTEM
   SHALL mark it unmatched, report it as an isolation failure, and SHALL NOT print its text.
3. WHEN the department given is not defined in `config/app.json` THE SYSTEM SHALL stop
   with an error before calling AWS.
