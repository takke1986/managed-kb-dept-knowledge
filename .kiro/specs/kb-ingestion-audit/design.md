# Design: KB ingestion audit

## Overview

A command-line audit that asks the knowledge base what it indexed and checks each answer
against the Markdown we sent. The comparison logic is pure Python with no AWS calls, so it
can be tested exhaustively; the AWS-facing part is a thin layer that only reads.

```
scripts/audit_ingestion.py   CLI: arguments, AWS clients, summary, exit status
audit/
  normalise.py               normalise(text) -> str                       (Req 1)
  matching.py                classify(chunk, source) -> Verdict           (Req 2)
  spans.py                   diff_spans(chunk, source) -> list[Span]      (Req 3)
  collect.py                 retrieve + de-duplicate chunks, load sources (Req 4, 5)
tests/                       pytest, plus Hypothesis for the properties below
```

## Architecture

```
operator ──► audit_ingestion.py
               │  1. sign in as a member of <department> (Cognito, same as scripts/verify.py)
               │  2. tools/call kb___Retrieve on the AgentCore Gateway (MCP), per query,
               │     filter {"equals": {"key": "department", "value": <department>}}
               │  3. GetObject kb-source/<department>/<doc_id>/<file>.md  (DocsBucket)
               ▼
            audit.collect ──► audit.matching ──► audit.spans
               ▼
            summary on stdout, optional --json, exit status 0/1
```

**Why the Gateway and not `bedrock-agent-runtime.retrieve` directly.** The KB's resource
policy denies `bedrock:Retrieve` to everything except the Gateway's role (isolation layer
④). Going through the Gateway as a department member means the audit sees exactly what a
member sees - the property we want to check - and cannot accidentally widen its view.

## Components and interfaces

### `audit.normalise`

```python
def normalise(text: str) -> str:
    """NFKC, then drop every character for which str.isspace() is true."""
```

NFKC folds full-width ASCII and digits (`１２` -> `12`) and compatibility forms. Whitespace
removal absorbs line re-flow from chunking. Both are layout, not content.

### `audit.matching`

```python
class Verdict(StrEnum):
    VERBATIM = "verbatim"
    ALTERED = "altered"
    SKIPPED = "skipped"      # image chunk, or empty after normalisation
    UNMATCHED = "unmatched"  # source missing, unreadable, or in another department

@dataclass(frozen=True)
class Result:
    chunk_id: str
    verdict: Verdict
    source_key: str | None
    reason: str = ""
    spans: tuple[Span, ...] = ()

def classify(chunk_text: str, metadata: dict, source_text: str | None) -> Verdict
```

Order of checks: image metadata -> empty -> source missing -> substring test. The substring
test is the only thing that can produce VERBATIM. No similarity threshold is used for the
verdict (`.kiro/steering/audit-matching.md`, rule 2).

### `audit.spans`

```python
@dataclass(frozen=True)
class Span:
    source: str   # <= 40 chars
    chunk: str    # <= 40 chars

def diff_spans(chunk_text: str, source_text: str, limit: int = 5) -> list[Span]
```

1. Normalise both.
2. Find the source window most similar to the chunk: anchor on the longest common block
   (`difflib.SequenceMatcher.find_longest_match`), then take a window of the chunk's length
   around it.
3. Run `SequenceMatcher` on window vs chunk and keep `replace`, `delete` and `insert`
   opcodes, truncating each side to 40 characters.

### `audit.collect`

```python
def source_key_for(chunk: dict) -> str | None
def collect_chunks(retrieve: Callable[[str], list[dict]], queries: list[str]) -> list[dict]
```

`source_key_for` prefers the chunk's S3 location URI (it is the `.md` key the KB indexed);
otherwise it derives the key from `metadata.original_key` with `DocRef` from
`functions/shared/common.py`, so the key format has a single definition.

`collect_chunks` de-duplicates on the chunk's location plus its text, because the same
chunk is commonly returned for several queries.

## Data models

Per-chunk JSON written by `--json`:

```json
{
  "chunk_id": "kb-source/legal/3f.../モデル就業規則.md#a1b2c3d4",
  "verdict": "altered",
  "source_key": "kb-source/legal/3f.../モデル就業規則.md",
  "reason": "",
  "spans": [{ "source": "膳所営業所", "chunk": "陸所営業所" }]
}
```

`chunk_id` is the source key plus the first 8 hex characters of the SHA-256 of the chunk's
normalised text, which is stable across runs.

## Error handling

| Situation | Behaviour |
|---|---|
| Department not in `config/app.json` | Exit 2 before any AWS call (Req 5.3) |
| `.state-outputs.json` missing | Exit 2 with the existing message from `scripts/project.py` |
| Gateway returns an error for one query | Report it, continue with the other queries, exit 1 at the end |
| Source object missing or unreadable | That chunk is UNMATCHED with the S3 error code as reason |
| Chunk from another department | UNMATCHED, reason `isolation`, chunk text never printed (Req 5.2) |

## Correctness properties

These are the properties the test suite checks with generated inputs. They map directly to
the acceptance criteria.

1. **Idempotence of normalisation** - for any string `s`,
   `normalise(normalise(s)) == normalise(s)`. *(Req 1.2)*
2. **Whitespace invariance** - inserting any whitespace anywhere in `s` does not change
   `normalise(s)`. *(Req 1.1, 1.3)*
3. **Width invariance** - replacing ASCII letters and digits in `s` with their full-width
   forms does not change `normalise(s)`. *(Req 1.3)*
4. **Every excerpt is verbatim** - for any source `t` and any slice `t[i:j]` whose
   normalisation is non-empty, `classify(t[i:j], {}, t) == VERBATIM`. *(Req 2.1)*
5. **A change is never verbatim** - for any source `t` and excerpt `e = t[i:j]`, if `e'`
   differs from `e` in one character and `normalise(e')` is not a substring of
   `normalise(t)`, then `classify(e', {}, t) == ALTERED`. *(Req 2.2)*
6. **Image chunks are always skipped** - for any texts, metadata with
   `_media_type: image` gives SKIPPED. *(Req 2.3)*
7. **Span bounds** - for any chunk and source, `diff_spans` returns at most `limit` spans,
   and every side of every span is at most 40 characters. *(Req 3.2, 3.3)*
8. **No spans for verbatim chunks** - if `classify(...) == VERBATIM` then `diff_spans`
   returns an empty list. *(Req 3.1)*
9. **De-duplication** - `collect_chunks` over queries that all return the same list
   returns that list once, regardless of the number of queries. *(Req 4.2)*

## Testing strategy

- **Unit tests** (`tests/test_*.py`): concrete Japanese cases taken from measurements -
  a full-width date, a proper noun substitution (`膳所営業所` -> `陸所営業所`), a
  blank-to-zero change (`○%` -> `0%`), an image chunk, a chunk from another department.
- **Property-based tests** (Hypothesis): the nine properties above.
- **End-to-end**: run the CLI against the deployed stack for each department. Because this
  project ingests Markdown only, the expected result is zero ALTERED.
