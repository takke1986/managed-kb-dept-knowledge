---
inclusion: fileMatch
fileMatchPattern: ["functions/convert/**", "functions/sync/**", "scripts/audit_*.py", "audit/**"]
---

# KB ingestion: only Markdown reaches the knowledge base

**Rule: never put a PDF, Word, PowerPoint, CSV or Excel file into the knowledge base's
source bucket. Transcribe it to Markdown first (`functions/convert`), and ingest the
Markdown.**

## Why

Managed Knowledge Bases always use `SMART_PARSING`, and it cannot be changed. For PDF,
Word, PowerPoint, CSV and Excel, the document is parsed once, indexed, and then parsed
again some minutes later by a multi-modal pass whose result **replaces** the first.
TXT, MD and HTML are parsed once.

Measured on Japanese documents, the replacement corrupts body text: proper nouns turn into
other plausible names, clauses go missing, and a 94-page document went from 100% of chunks
matching the source verbatim to under 15%. The corruption is not visible from the chunk
alone - the substituted text is fluent Japanese.

Markdown takes the single-pass path, so what we transcribe is what gets indexed.

## Example

```python
# Good: the KB source only ever receives the transcription (DocRef.text_key
# is kb-source/<dept>/<doc_id>/<file>.md, see functions/shared/common.py)
s3.put_object(Bucket=BUCKET, Key=doc.text_key,
              Body=markdown.encode("utf-8"), ContentType="text/markdown; charset=utf-8")

# Bad: the original file goes straight into the KB source and gets re-parsed
s3.copy_object(Bucket=BUCKET, Key=f"kb-source/{dept}/{doc_id}/{name}.pdf", CopySource=...)
```

## How to check it held

Run the ingestion audit (`scripts/audit_ingestion.py`). It compares every retrieved
chunk with the Markdown we sent and reports any chunk that is not a verbatim excerpt.
A clean run is the evidence that this rule is working.
