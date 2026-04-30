# Anthropic SDK Major Bump: 0.40 → 0.97

> **For agentic workers:** Single-task plan. Dispatch as one implementer (haiku) — research showed this is much lower risk than the version-number leap suggests.

**Goal:** Upgrade `anthropic` from `>=0.40,<0.41` to `>=0.97,<0.98`. 57 minor releases of accumulated change, but **none of our integration points are touched**.

**Risk level:** LOW. Confirmed via the upstream CHANGELOG (research dated 2026-04-30):
- `AsyncAnthropic(api_key=...)` — unchanged
- `messages.create(model, max_tokens, system=[{type, text, cache_control}], tools, tool_choice, messages)` — unchanged
- `response.content[i].type == "tool_use"` + `block.input` — unchanged
- `response.usage.input_tokens` / `output_tokens` — unchanged (additive cache_creation/cache_read fields appear but aren't required reads)
- Model literal `claude-haiku-4-5-20251001` — still valid; not in any deprecation list through 0.97

**The relevant breaking changes between 0.40 and 0.97:**

| Version | Change | Affects us? |
|---|---|---|
| 0.41 | Removed deprecated HTTP client kwargs (`proxies=`, etc.) | No — we don't pass any |
| 0.50 | Streaming event schemas extracted | No — we don't stream |
| 0.59 | Removed deprecated model string literals from `anthropic.types` enums | No — runtime literal pin still works |
| 0.68 | `NotGiven` → `Omit` rename in TypedDict stubs | No — we don't import these |
| 0.72 | Dropped Python 3.8 (min: 3.9) | No — project is on 3.14 |
| 0.77 | Beta `output_format` → `output_config` | No — we don't use that beta |
| 0.82 | `UserLocation` type restructure | No — we don't use it |
| 0.84 | Multipart array encoding format change | No — we don't upload files |

**Branch:** `chore/anthropic-sdk-bump`. Cut from `main`.

**Soak window:** 7 days on `main` post-merge before tagging anything depending on it (per dependency-hygiene memory).

---

## Files Touched

- `pyproject.toml` — bump `anthropic` constraint AND remove the mypy override (the SDK now ships `py.typed` with proper inline stubs).
- `src/trip_tracker/parsers/llm.py` — try removing the `# type: ignore[call-overload, unused-ignore]` on `messages.create(...)`. With proper stubs, mypy may now accept the call directly.
- `tests/test_parsers_llm.py` — verify the `_fake_response` MagicMock builder still works (no changes expected).
- `tests/test_parsers_llm_live.py` — run after the bump with a real API key.

No code-shape changes expected.

---

## Migration Steps

- [ ] **Step 1 — Bump the pin**

  ```bash
  uv add 'anthropic@^0.97'
  uv lock --upgrade-package anthropic
  ```

  Confirm `pyproject.toml` shows `>=0.97,<0.98`.

- [ ] **Step 2 — Drop the mypy override**

  In `pyproject.toml`, find:

  ```toml
  [[tool.mypy.overrides]]
  module = ["anthropic", "anthropic.*"]
  ignore_missing_imports = true
  ```

  Delete this block. The 0.97 SDK ships a `py.typed` PEP 561 marker.

- [ ] **Step 3 — Try removing the type-ignore comment**

  In `src/trip_tracker/parsers/llm.py:39`:

  ```python
  return await self._client.messages.create(  # type: ignore[call-overload, unused-ignore]
  ```

  Delete the `# type: ignore[...]` comment. Run `uv run mypy src` — if mypy accepts the call directly with proper Haiku 4.5 model literal coverage, leave it removed. If mypy complains, restore with a tighter ignore (`[call-overload]` only).

- [ ] **Step 4 — Run the mocked unit tests**

  ```bash
  uv run pytest tests/test_parsers_llm.py -v
  ```

  Three tests. Should pass without modification — the `_fake_response` MagicMock shape is decoupled from the SDK's internal types.

- [ ] **Step 5 — Live smoke test**

  ```bash
  ANTHROPIC_API_KEY=sk-ant-... uv run pytest -m live_llm -v
  ```

  Round-trips a canonical email through real Haiku. Catches prompt-template + tool-schema bugs that mocked tests miss. Ad-hoc cost: ~$0.005.

- [ ] **Step 6 — Run full suite + lint**

  ```bash
  uv run pytest --cov
  uv run ruff check . && uv run mypy src
  uv run pre-commit run --all-files
  ```

  Coverage gate stays at 85%.

- [ ] **Step 7 — Commit + push**

  ```bash
  git add pyproject.toml uv.lock src/trip_tracker/parsers/llm.py
  git commit -m "chore(deps): bump anthropic 0.40 → 0.97; drop mypy override"
  git push origin chore/anthropic-sdk-bump
  gh pr create --fill
  ```

---

## What Could Go Wrong (and how we'd notice)

| Failure mode | Detection | Mitigation |
|---|---|---|
| mypy rejects `messages.create` call after override removed | `uv run mypy src` fails | Restore mypy override OR keep tighter `# type: ignore[call-overload]` |
| Mocked unit tests break (response shape diverges from MagicMock) | `pytest tests/test_parsers_llm.py` red | Adjust `_fake_response` to match new SDK types |
| Live LLM test fails (prompt or tool schema rejected) | `pytest -m live_llm` red | Inspect the actual response error; likely `cache_control` format change. Restore prior format |
| Worker crashes at runtime | Container restart loop in production | Rollback (see below); investigate logs |

---

## Rollback Plan

The Anthropic SDK is **stateless**: HTTP calls in, typed objects out. No DB migrations, no on-disk state, no Redis state, no cron behaviors. Rolling back is a one-line revert.

**If discovered before tag:**

```bash
git revert <bump-commit-sha>
uv lock
git push origin main
```

**If discovered after tag pushed:**

1. `git revert <bump-commit-sha>` on main, push.
2. The next tag (e.g., v0.3.1) ships with anthropic pinned back to 0.40.
3. The release-verification scheduled agent (same pattern as v0.3.0) confirms the GHCR image rebuild.

**State that needs cleanup on rollback:** None. The `llm_budget` Postgres table tracks daily Haiku spend in cents; it's version-agnostic.

**Revert blast radius:** trip-tracker only — no other consumers of this code.

---

## Done Definition

- All tests pass (≥85% coverage).
- mypy + ruff + bandit + djlint + pre-commit clean.
- Docker build succeeds.
- Live-LLM smoke test passes against real Haiku.
- 7-day soak on main.
- Renovate dashboard no longer flags anthropic.
