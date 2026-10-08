# Home Hub

A FastAPI host (`hub/`) that loads self-contained plugin apps from `apps/<id>/`.

- **Building or changing a plugin:** follow `docs/plugin-guide.md`. It is the
  source of truth for the plugin contract, the `ctx` services, the UI
  components and the rules.
- A plugin touches the host only through `ctx`. Don't edit `hub/` or other
  plugins to build a plugin.
- If you change the host's plugin-facing API (`hub/plugin.py`,
  `hub/services/*`, `hub/templates/components.html`), update
  `docs/plugin-guide.md` in the same change. `tests/test_docs.py` fails if
  something is undocumented.
- Installing the hub, installing plugins, and how migrations run:
  `docs/installation.md`. If you change dependencies in `pyproject.toml`,
  regenerate `requirements.txt` / `requirements-dev.txt` (the commands are in
  that doc).
- Finance email parsing (regex rules in the database, scores, the miss log, the
  regex compiler): `docs/finance-parsing.md`. Update it when you change
  `apps/finance/parsers.py`, `amazon.py`, `learn.py` or how misses are handled.
- Run `uv run pytest` before committing. Run the server with
  `uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000`.
