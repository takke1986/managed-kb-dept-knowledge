---
inclusion: always
---

# Product

A department-scoped knowledge base built on Amazon Bedrock **Managed** Knowledge Bases.
Each department (sales, legal, ...) stores files, and staff ask questions that are
answered only from their own department's documents.

## Who uses it

- Staff in a Japanese enterprise. Documents are Japanese: work rules, contracts,
  quotations, invoices. Answers must be exact, because a wrong number in a work rule or
  contract is worse than no answer.
- People can belong to more than one department. Their answers may use every
  department they belong to, and nothing else.

## Non-negotiables

- **Department isolation is the product.** A sales user must never receive legal's
  content - not in an answer, not in a citation, not in a file listing. It is enforced
  in four independent layers (identity, Cedar policy, KB metadata filter, IAM). Never
  remove a layer because another one "already covers it".
- **Prefer saying "not found" to guessing.** The agent declines when the retrieved
  material does not contain the answer.
- **Japanese text must survive ingestion verbatim.** See `kb-ingestion.md`.
