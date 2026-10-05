---
inclusion: fileMatch
fileMatchPattern: ["kbaudit/**", "scripts/audit_*.py", "tests/**"]
---

# Comparing retrieved chunks with their source

When code decides whether a chunk "matches" its source, it must follow these rules.
They came from measurement: each one removed a false alarm or a missed corruption.

1. **Normalise both sides the same way before comparing:** Unicode NFKC, then remove all
   whitespace. Chunking re-flows lines, and NFKC folds full-width digits (`１２`) into
   ASCII (`12`) - that is layout, not corruption.
2. **A chunk matches only if its normalised text is a substring of the normalised
   source.** Do not use a similarity ratio as the verdict; a 95%-similar chunk can still
   have the one number that matters changed.
3. **Skip image chunks.** A chunk whose metadata has `_media_type: image` is a
   generated description of a figure. It is expected not to appear in the source.
4. **Report the smallest differing span, not the whole chunk**, so a reviewer can see
   `膳所営業所 -> 陸所営業所` instead of reading 800 characters.
5. **Never write expected values in a normalised form in tests.** Write them the way the
   document writes them (`2026年9月30日`, not `2026-09-30`), and let the normaliser do
   its job. A test that pre-normalises the expectation hides normaliser bugs.

## Example

```python
# Good
def matches(chunk: str, source: str) -> bool:
    return normalise(chunk) in normalise(source)

# Bad: 0.97 similarity passes even when "70歳" became "65歳"
def matches(chunk: str, source: str) -> bool:
    return SequenceMatcher(None, chunk, source).ratio() > 0.95
```
