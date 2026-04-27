## Summary

-

## Context

Closes #

## Test plan

- [ ] `uv run pytest` passes locally
- [ ] `uv run ruff check . && uv run mypy` clean
- [ ] If touching templates: `uv run djlint src/trip_tracker/templates --check`
- [ ] If touching Docker: `docker build .` succeeds
- [ ] CI green
