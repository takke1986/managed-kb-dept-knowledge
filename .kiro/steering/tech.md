---
inclusion: always
---

# Tech stack and conventions

| Area | Choice |
|---|---|
| Infrastructure | AWS CDK (TypeScript), `cdk/`. Region `ap-northeast-1`; the WAF stack alone is `us-east-1` |
| Functions | Python 3.13 Lambdas, `functions/<name>/index.py`, shared code in `functions/shared/` |
| Scripts | Python, run with `uv run python scripts/...`. Dependencies in `pyproject.toml` |
| Web | Vite + React + Amplify UI (Storage Browser), `web-app/` |
| Models | `jp.` inference profiles only, so documents stay in Japan |

## Rules

- **Use `uv`, never bare `pip`.** Lambda dependencies are installed per function by
  `scripts/build_functions.sh`, not from `pyproject.toml`.
- **Lint with ruff before committing:** `uv run ruff check functions scripts`.
  Long Japanese lines are allowed (`E501` is ignored on purpose); do not wrap them.
- **Configuration lives in `config/app.json`** (departments, tags, models, prefix).
  Do not hard-code a department id, a bucket name, or an account id anywhere.
- **No secrets or account identifiers in the repository.** Use `${this.account}` in CDK
  and read stack outputs from `.state-outputs.json` (git-ignored) in scripts.
- **Comments explain why, not what.** The codebase writes comments in Japanese and in
  full sentences; match that style when editing existing files.

## Example

```python
# Good: the department comes from configuration, not from a literal
from shared.common import DEPARTMENTS   # built from config/app.json
for dept in DEPARTMENTS:
    ...

# Bad: a literal department id breaks the moment config/app.json changes
for dept in ["sales", "legal"]:
    ...
```
