# wombat

A personal assistant agent built on the [**cog-worx**](../cog_worx/cog-worx) cognitive-architecture
framework — the loop, memory, verification, and perception, out of the box.

> Status: bootstrap. Project skeleton + local toolchain wired; the agent itself is TBD.

## Develop

```bash
uv sync                 # install (Python 3.13+); cog-worx resolves from ../cog_worx/cog-worx (editable)
uv run wombat           # run the entrypoint
uv run ruff check .     # lint
uv run mypy             # type-check (strict)
uv run pytest           # tests
```

cog-worx is wired as a **local editable directory source** (`[tool.uv.sources]` in `pyproject.toml`),
so edits in the sibling `cog_worx/cog-worx` checkout are picked up live. Swap that for a version or git
pin once cog-worx is published.

## Tooling

- **codebase-pkg** — queryable Neo4j knowledge graph of this repo, exposed to Claude Code over MCP.
  Bring up the graph with `docker compose -f docker-compose.codebase-pkg.yml up -d` then `codebase-pkg seed`.
- **memory** — TBD. Selecting a free, model-free long-term memory MCP server (no LLM/embedding calls).
