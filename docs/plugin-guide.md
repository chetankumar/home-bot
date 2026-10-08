# Home Hub plugin guide

This is the complete reference for building a Home Hub plugin (an "app"). It
covers the folder layout, the contract with the host, every service the host
provides, the shared UI, configuration, testing, and the rules a plugin must
follow. You should not need to read the host source (`hub/`) to build a
plugin. If you do, this guide is missing something.

Installing the hub, installing someone else's plugin, and how migrations are
run in production are covered in [installation.md](installation.md).

- [1. What a plugin is](#1-what-a-plugin-is)
- [2. Quick start](#2-quick-start)
- [3. Folder layout](#3-folder-layout)
- [4. The manifest](#4-the-manifest)
- [5. setup(ctx) and the load lifecycle](#5-setupctx-and-the-load-lifecycle)
- [6. AppContext reference](#6-appcontext-reference)
- [7. UI: templates, components, HTMX](#7-ui-templates-components-htmx)
- [8. Routes](#8-routes)
- [9. Rules](#9-rules)
- [10. Configuration](#10-configuration)
- [11. Testing a plugin](#11-testing-a-plugin)
- [12. Checklist before shipping](#12-checklist-before-shipping)
- [13. Troubleshooting](#13-troubleshooting)
- [14. Worked examples in this repo](#14-worked-examples-in-this-repo)

---

## 1. What a plugin is

- A plugin is a Python package in its own folder: `apps/<id>/`.
- The host imports it **in-process at startup**, calls its `setup(ctx)`, and
  mounts the router it returns at **`/apps/<id>/`**.
- Adding a plugin means adding a folder. You never edit the host (`hub/`) or
  another plugin.
- Plugins are loaded once. **Restart the hub** to pick up new or changed code
  (`python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000` in the conda
  env, or `uv run uvicorn ...`).
- Each plugin is isolated: a plugin that fails to import, or whose `setup()`
  raises, is marked *failed*. The launcher and `/admin` show it with its
  traceback, and the host and every other plugin keep running.
- The UI is server-rendered Jinja2 with HTMX. There is no JavaScript build
  step.
- All host services are **synchronous**: SQLite, httpx and the AI SDKs are
  called in blocking style. Write sync route handlers (`def`, not
  `async def`). FastAPI runs them in a thread pool.

## 2. Quick start

The fastest route is to copy the reference app:

```bash
cp -r apps/hello apps/plants          # Windows: xcopy /E /I apps\hello apps\plants
# then edit apps/plants/__init__.py: manifest.id = "plants", name, icon, description
python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

Or write one from scratch. The four files below are a complete, working
plugin. (They are also loaded by the test suite, so they are known to work.)

<!-- example: plants/__init__.py -->
```python
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext, Manifest

manifest = Manifest(
    id="plants",                     # must equal the folder name
    name="Plants",
    icon="🪴",
    description="Track when each plant was last watered.",
)


def setup(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with ctx.db() as conn:
            plants = conn.execute("SELECT * FROM plants ORDER BY name").fetchall()
        return ctx.render(request, "plants.html", plants=plants)

    @router.post("/plants")
    def add_plant(name: str = Form(...)):
        with ctx.db() as conn:
            conn.execute("INSERT OR IGNORE INTO plants(name) VALUES (?)", (name.strip(),))
        return RedirectResponse(ctx.url("/"), status_code=303)

    return router
```

<!-- example: plants/migrations/001_init.sql -->
```sql
CREATE TABLE plants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    watered_at TEXT
);
```

<!-- example: plants/templates/plants.html -->
```jinja
{% extends "base.html" %}
{% import "components.html" as ui %}
{% block content %}
<h1>{{ app.icon }} {{ app.name }}</h1>
{% call ui.card("Plants") %}
  <form method="post" action="{{ app_url('/plants') }}" class="row">
    <input name="name" placeholder="Plant name" required>
    <button>Add</button>
  </form>
  {% for p in plants %}
    <p>{{ p.name }} <span class="muted">watered {{ p.watered_at | dt }}</span></p>
  {% else %}
    {{ ui.empty("No plants yet.") }}
  {% endfor %}
{% endcall %}
{% endblock %}
```

Restart the hub. Then:

- open `/apps/plants/`;
- check `/admin`, which should show the app as **loaded**;
- look for `data/apps/plants.db`.

## 3. Folder layout

```
apps/<id>/
  __init__.py          required: `manifest` and `setup(ctx)`
  migrations/          optional: NNN_name.sql files, applied in numeric order
    001_init.sql
  templates/           optional: Jinja templates (searched before the hub's)
    <id>.html
    _partial.html      convention: leading underscore = HTMX partial
  static/              optional: served at /apps/<id>/static/
  requirements.txt     optional: extra pip packages (see "Python dependencies")
  routes.py, sync.py…  optional: your own modules
```

- **Imports inside your plugin must be relative** (`from . import routes`,
  `from .stats import inr`). The host loads the package under the synthetic
  name `hub_apps.<id>`, so an absolute import such as `import apps.plants.x`
  would load a second copy of the module.
- Folders starting with `_` or `.`, and folders without an `__init__.py`,
  are ignored.
- **Python dependencies.** Everything the hub installs can be imported
  freely: FastAPI, httpx, Pydantic, Jinja2, APScheduler, cryptography, and
  the anthropic, openai and google-genai SDKs. Prefer these. If you need
  another package:
  - list it, pinned, in `apps/<id>/requirements.txt`;
  - mention it in your plugin's docstring.

  Whoever installs the plugin runs `python -m pip install -r apps/<id>/requirements.txt`
  (see [installation.md](installation.md#part-2-install-a-plugin)). The hub
  does not install packages itself. A missing package shows up as a
  `ModuleNotFoundError` on `/admin`, and only your plugin fails.
- Heavy imports can go inside `setup()` (see `apps/finance/__init__.py`), so
  that an import error is reported as that plugin failing.

## 4. The manifest

```python
from hub.plugin import Manifest

manifest = Manifest(
    id="plants",          # str, required. ^[a-z][a-z0-9_]*$ and equal to the folder name
    name="Plants",        # str, required. Shown in the nav bar and launcher
    icon="🪴",            # str, default "🧩". One emoji
    description="...",    # str, default "". One line, shown on the launcher
    requires=[],          # list[str], default []. Host connections this plugin needs
    version="0.1.0",      # str, default "0.1.0". Informational
)
```

### `requires`

`requires` declares host-provided connections. These are the known values:

| Value | Gives you | Connected via |
|---|---|---|
| `"gmail"` | `ctx.gmail` (read-only Gmail) | `/connections`: one-time Google consent on the host PC |

- An unknown value makes the plugin fail to load, with the error
  `unknown requirement(s)`.
- A requirement that is declared but **not yet connected** doesn't stop the
  plugin loading. The launcher shows a "needs Gmail connection" badge, and
  calls raise `NotConnected` until the connection is made. Check
  `ctx.gmail.connected` and guide the user to `/connections`.

## 5. setup(ctx) and the load lifecycle

```python
def setup(ctx: AppContext) -> APIRouter: ...
```

At startup, for each `apps/<id>/` that is not in `hub.toml`'s `disabled`, the
host:

1. Imports `hub_apps.<id>` (your `__init__.py`).
2. Validates the plugin. It needs a module-level `manifest` (a `Manifest`)
   and a callable `setup`. `manifest.id` must equal the folder name, and
   every `requires` entry must be known.
3. Opens `data/apps/<id>.db` and **applies your pending migrations**.
4. Builds the `AppContext`.
5. Calls `setup(ctx)`. This must return a `fastapi.APIRouter`.
6. Mounts `static/` at `/apps/<id>/static`, then includes your router at
   `/apps/<id>`.

Any exception in steps 1–6 marks the plugin **failed** and records the
traceback. Jobs registered during a failed `setup()` are removed. Routes are
mounted only after `setup()` succeeds, so a failed plugin never has half its
routes live. Its URLs show an error page (HTTP 503) instead.

What belongs in `setup()`:

- Do: register scheduled jobs, add Jinja filters, define routes, and read
  config.
- Don't: make network calls, call AI, or do slow work. `setup()` runs during
  host startup. Schedule that work as a job, or do it on first request.

## 6. AppContext reference

`ctx` is the **only** way a plugin talks to the host. Everything on it is
scoped to your plugin.

| Member | Type | Summary |
|---|---|---|
| `ctx.id` | `str` | Your plugin id |
| `ctx.manifest` | `Manifest` | Your manifest |
| `ctx.path` | `Path` | Your plugin folder (`apps/<id>`) |
| `ctx.db` | `Database` | `with ctx.db() as conn:` opens your SQLite file |
| `ctx.ai` | `AppAI` | LLM calls, with your hub.toml policy enforced |
| `ctx.http` | `AppHttp` | Preconfigured outbound HTTP clients |
| `ctx.gmail` | `GmailClient` | Read-only Gmail (only if `requires=["gmail"]`) |
| `ctx.scheduler` | `AppScheduler` | Cron and interval jobs with run history |
| `ctx.kv` | `KV` | Small JSON settings store |
| `ctx.charts` | `AppCharts` | Period statistics and a ready-made progress chart |
| `ctx.templates` | `Jinja2Templates` | Your Jinja environment |
| `ctx.render(...)` | method | Render a template to a response |
| `ctx.tz` | `ZoneInfo` | Hub timezone (e.g. Asia/Kolkata) |
| `ctx.log` | `logging.Logger` | Logger named `apps.<id>` |
| `ctx.config` | `dict` | Your `[apps.<id>]` table from hub.toml (without `ai`) |
| `ctx.prefix` | `str` | `"/apps/<id>"` |
| `ctx.url(path)` | method | `ctx.url("/x")` → `"/apps/<id>/x"` |

### 6.1 `ctx.db`: your SQLite database

```python
with ctx.db() as conn:                     # sqlite3.Connection
    conn.execute("INSERT INTO plants(name) VALUES (?)", ("Fern",))
    rows = conn.execute("SELECT * FROM plants").fetchall()
    rows[0]["name"]                        # rows are sqlite3.Row (index by name or position)
```

- Each plugin gets its own file, `data/apps/<id>.db`. No other plugin can
  see it, and you must not open another plugin's file.
- `with ctx.db() as conn:` **commits** on normal exit, **rolls back** if an
  exception is raised, and **always closes** the connection. Open one per
  unit of work and don't keep connections around.
- Connection settings:
  - WAL journal mode;
  - `PRAGMA foreign_keys=ON`;
  - 30 s busy timeout;
  - `check_same_thread=False`.

  WAL makes concurrent reads from request threads and job threads safe.
- `ctx.db.path` is the file's `Path`. `ctx.db.connect()` is an alias of
  `ctx.db()`.
- Always use parameters (`?`), never string formatting, for values.

**Migrations.** Put schema in `migrations/NNN_name.sql`:

- Files must match `^\d+_[\w-]+\.sql$`. They are sorted by the **number**,
  so `2_x.sql` runs before `10_y.sql`. Duplicate numbers are an error.
- Each file runs **once**, inside a transaction, and is recorded in a
  `_migrations` table inside your DB file. If a migration fails, nothing
  from it is applied and the plugin fails to load.
- **The hub runs migrations, not you or your code.** It runs them
  automatically at every startup, before `setup()`. There is no migrate
  command to call, and plugin code must not create or alter tables itself.
  The console logs `app <id>: applied migrations …`, and `/admin` shows each
  plugin's latest migration. Full details are in
  [installation.md, Part 3](installation.md#part-3-migrations-who-runs-them-and-when).
- **Never edit a migration that has already run.** Add a new numbered file
  instead, e.g. `002_add_notes.sql` containing
  `ALTER TABLE plants ADD COLUMN notes TEXT;`.
- Seed data (default categories etc.) can go in a migration as `INSERT`s.
- Recommended conventions:
  - store money as integer minor units (paise or cents);
  - store times as ISO 8601 strings, choosing local time (`ctx.tz`) or UTC
    and documenting which in the migration;
  - give booleans `INTEGER NOT NULL DEFAULT 0`.

### 6.2 `ctx.ai`: language models

```python
from hub.services.ai import AIError, AIPolicyError, ExtractionError, Message, ProviderUnavailable
```

#### `complete()`

```python
result = ctx.ai.complete(
    messages,                 # str | list[Message] | list[{"role": ..., "content": ...}]
    model=None,               # alias ("fast") or "provider:model"; None = your default
    system=None,              # optional system prompt
    max_tokens=1024,
    temperature=None,         # None = provider default
)
result.text                   # str
result.provider, result.model # which model actually answered
result.usage.input_tokens, result.usage.output_tokens   # may be None
```

#### `stream()`

```python
for chunk in ctx.ai.stream(messages, model=None, system=None, max_tokens=1024, temperature=None):
    ...                       # str pieces of the reply
```

The policy is checked when you call `stream()`, before iteration starts.
Errors during streaming raise `AIError` from the iterator.

#### `extract()`: typed structured output

```python
from pydantic import BaseModel

class Reminder(BaseModel):
    title: str
    due: str | None = None    # "YYYY-MM-DD"

r = ctx.ai.extract(
    "Remind me to repot the fern next Sunday",
    schema=Reminder,          # required, keyword-only
    model=None,
    system="Today is 2026-10-03.",
    max_tokens=1024,
)
assert isinstance(r, Reminder)
```

- `extract()` uses each provider's native structured output:
  - Anthropic: forced tool use;
  - OpenAI, OpenRouter, LM Studio and Ollama: a JSON-schema `response_format`;
  - Gemini: a response schema.

  It then validates the result with your Pydantic model. Fenced
  ```` ```json ```` output from small local models is accepted.
- If validation fails, it retries **once**, showing the model its error. If
  that also fails, it raises `ExtractionError`.
- Put cross-field rules in a Pydantic `model_validator` (see
  `apps/finance/extract.py`). A validation error there triggers the same
  retry.

#### Other members

- **`ctx.ai.available(model=None) -> bool`**: whether the model resolves,
  your policy allows it, and its provider has a key. Use this to show or
  hide AI features in the UI.
- **`ctx.ai.default_model`**: your default alias (from policy, else
  `"default"`).
- **`ctx.ai.resolve(model=None) -> tuple[str, str]`**: what an alias (default:
  yours) points to, as `(provider, model)`. It doesn't check policy or keys,
  and raises `AIError` for an unknown alias. Handy for showing "Using
  lmstudio:qwen2.5-7b-instruct" on a settings page.
- **`ctx.ai.allowed_providers`**: `list[str] | None` (`None` means any).

#### Models and aliases

Models are referenced as `"provider:model"`. The string is split on the
**first** colon, so `"ollama:llama3.1:8b"` works. You can also use an alias
from `[ai.aliases]` in `hub.toml`. The shipped aliases are:

| Alias | Points to (default config) |
|---|---|
| `default` | `anthropic:claude-opus-5-5` |
| `fast` | `anthropic:claude-haiku-4-5-20251001` |
| `local` | A local model server: `lmstudio:<model identifier>` (shipped default) or `ollama:<model>` |

The configured providers are `anthropic`, `openai`, `openrouter` and
`gemini` (cloud), and `lmstudio` and `ollama` (local, no key needed). Any
other OpenAI-compatible server can be added in `hub.toml` as
`[ai.providers.<name>]` with `type = "openai_compat"`, its `base_url`, and
`key_required = false`. **Prefer aliases** over hard-coded model ids, so the owner can
re-point them in one place.

#### Policy

Policy is enforced by the host, not by your code. If `hub.toml` has:

```toml
[apps.plants.ai]
allowed_providers = ["lmstudio", "ollama"]   # omit = any provider
default_alias = "local"          # omit = "default"
```

then any call that resolves to another provider raises `AIPolicyError`
**before any network request**. Use this for plugins that handle private
data. Listing both local servers means the owner can switch between LM Studio
and Ollama by changing only the `local` alias.

#### Errors

All errors subclass `AIError`, so `except AIError` catches everything:

| Exception | When |
|---|---|
| `AIPolicyError` | Provider not in your `allowed_providers` |
| `ProviderUnavailable` | Provider not configured, or no API key |
| `ExtractionError` | Structured output failed validation twice |
| `AIError` | Anything else (unknown alias, network error, provider error), with the original as `__cause__` |

AI calls can be slow (seconds to minutes for local models) or fail. Always
catch `AIError` and degrade gracefully, and do bulk AI work in a scheduled
job rather than a request.

Every call is logged with your app id, provider, model and tokens. You can
see it on `/admin` under "AI usage by app".

### 6.3 `ctx.http`: outbound APIs

```python
with ctx.http.client("weather") as client:          # httpx.Client
    resp = client.get("/weather", params={"q": "Bengaluru"})
    resp.raise_for_status()
    data = resp.json()
```

- `ctx.http.client(name, **overrides) -> httpx.Client` returns a client
  preconfigured from `[connections.<name>]` in `hub.toml` (base URL, auth,
  headers, timeout). Keyword overrides are passed through to `httpx.Client`
  (e.g. `timeout=60`).
- Always use it in a `with` block, so the connection pool is closed.
- Raises `KeyError` (`UnknownConnection`) if the name isn't in hub.toml.
- Raises `PermissionError` if your `[apps.<id>]` table has a `connections`
  allow-list that doesn't include the name.
- Credentials never appear in plugin code; see
  [10. Configuration](#10-configuration).
- For a public API with no key, you may use `httpx` directly, but a named
  connection keeps base URLs and timeouts configurable.

### 6.4 `ctx.gmail`: read-only Gmail

This is only available when your manifest has `requires=["gmail"]`.
Accessing it otherwise raises `AttributeError`.

```python
from hub.services.gmail import GmailMessage, NotConnected

if not ctx.gmail.connected:            # bool: has the user done the one-time consent?
    ...                                # show a link to /connections

ids = ctx.gmail.search("from:(alerts@bank.com) after:1759257000", limit=2000)  # list[str], newest first
for mid in ctx.gmail.iter_ids("label:receipts", page_size=100):                 # lazy, paginates
    ...
msg: GmailMessage = ctx.gmail.get_message(ids[0])
msg.id, msg.thread_id
msg.received_at        # datetime, UTC (Gmail internalDate)
msg.sender             # bare address, lower-cased
msg.subject
msg.body               # decoded plain text; HTML-only mail is converted to text
msg.snippet
ctx.gmail.profile()    # {"emailAddress": ..., ...}
```

- The query uses [Gmail search syntax](https://support.google.com/mail/answer/7190).
  `after:` accepts epoch seconds, which is the most precise.
- The access scope is `gmail.readonly`. You can't send, modify or delete
  mail.
- Tokens refresh automatically. If the user revoked access, or never
  connected, calls raise `NotConnected`. Catch it, or let a scheduled job
  fail; the error then shows in the job's run history.
- Store raw message bodies in your DB if you parse them, so you can re-parse
  later without re-fetching (see `apps/finance/sync.py`).
- Treat ids as stable keys and make imports idempotent (use
  `INSERT OR IGNORE` on the Gmail id).

### 6.5 `ctx.scheduler`: background jobs

```python
def refresh():                         # no arguments; capture ctx via closure
    ...

ctx.scheduler.cron("refresh", refresh, "15 7 * * *")   # standard 5-field crontab, hub timezone
ctx.scheduler.interval("poll", refresh, minutes=15)    # kwargs: weeks, days, hours, minutes, seconds

ctx.scheduler.run_now("refresh")                # -> bool. Runs now in a background thread; False if already running
ctx.scheduler.run_now("refresh", wait=True)     # blocks until finished (handy in tests)
ctx.scheduler.is_running("refresh")             # -> bool
ctx.scheduler.last_run("refresh")               # -> dict | None
```

`last_run()` returns a dict like this:

```python
{"id": 12, "app_id": "plants", "job_id": "refresh",
 "trigger": "schedule" | "manual",
 "status": "running" | "ok" | "error" | "interrupted",
 "started_at": "2026-10-03T07:15:00+05:30", "finished_at": "...", "error": "<traceback or None>"}
```

- Register jobs in `setup()`. Job ids are namespaced per plugin, so
  `"refresh"` won't clash with another plugin's `"refresh"`.
- The function takes **no arguments**. Use a closure or `lambda` to capture
  `ctx`.
- A job never overlaps itself. A scheduled run or `run_now()` that arrives
  while the job is running is skipped (`run_now()` returns `False`).
- Exceptions are caught, logged to `apps.<id>`, and stored in the run
  history with the traceback. They never crash the host. To mark a run as
  failed, **raise**.
- Missed runs (e.g. the PC was asleep) fire once on wake if they are within
  an hour, and repeated misses are coalesced into one run.
- Jobs run in threads. Use `ctx.db()` inside the job, never a connection
  created outside it.
- `/admin` lists every job with its next run, last status and a **Run now**
  button.
- For a "Sync now" button with live status, see the pattern in
  [7.4](#74-htmx-patterns).

### 6.6 `ctx.kv`: small settings

```python
ctx.kv.get("budget_paise", default=None)   # -> Any (JSON-decoded) or default
ctx.kv.set("budget_paise", 3000000)        # any JSON-serialisable value
ctx.kv.delete("budget_paise")
ctx.kv.all()                               # -> dict[str, Any]
```

- Namespaced per plugin and stored in `hub.db`. Each call opens its own
  connection.
- Use it for user preferences and small state (a budget, a list of sender
  addresses, the last sync summary). Use `ctx.db` for anything tabular or
  growing.

### 6.7 `ctx.render` and `ctx.templates`

```python
return ctx.render(request, "plants.html", plants=rows)                 # TemplateResponse
return ctx.render(request, "_row.html", status_code=201, row=row)
return ctx.render(request, "_status.html", headers={"HX-Refresh": "true"}, **context)
```

- `ctx.render(request, name, status_code=200, headers=None, **context)`.
  `app` (your manifest) is added to the context automatically.
- `ctx.templates` is a Starlette `Jinja2Templates`. `ctx.templates.env` is
  the Jinja `Environment`. Add filters and globals in `setup()`:

  ```python
  ctx.templates.env.filters["inr"] = format_inr
  ctx.templates.env.globals["CATEGORIES"] = [...]
  ```

- **Lookup order**: your `templates/` first, then the hub's. So
  `"base.html"` and `"components.html"` resolve to the hub's, unless you
  shadow them (don't).
- Autoescaping is on for `.html`. `trim_blocks` and `lstrip_blocks` are on.

Template globals available everywhere:

| Name | Value |
|---|---|
| `request` | The current request (in the page context only, not inside imported macros) |
| `app` | Your `Manifest` (`app.name`, `app.icon`, …) |
| `app_url(path="/")` | `"/apps/<id>" + path`. Use this for every link and form action |
| `app_static(file)` | `"/apps/<id>/static/<file>"` |
| `hub_nav()` | Loaded apps for the top bar (used by `base.html`) |
| `hub_tz` | Hub `ZoneInfo` |

The built-in filter is `dt`:

```jinja
{{ value | dt }}                    {# "03 Oct 2026, 14:22" in the hub timezone #}
{{ value | dt("%d %b") }}           {# custom strftime #}
```

It accepts a datetime, an ISO string or epoch seconds, and shows `—` for
`None` or `""`. Aware datetimes are converted to the hub timezone. Naive ones
and ISO strings without an offset are shown as-is.

### 6.8 The rest

- **`ctx.tz`**: a `ZoneInfo` from `hub.toml`'s `timezone`. For "today", use
  `datetime.now(ctx.tz).date()`, never `date.today()` (the server may run in
  UTC).
- **`ctx.log`**: logs as `apps.<id>`; it appears in the hub's console
  output.
- **`ctx.config`**: your `[apps.<id>]` table, without `ai`.
  `ctx.config.get("sync_cron", "15 7 * * *")`. Always give a default.
- **`ctx.url(path)`** and **`ctx.prefix`**: build redirects with
  `RedirectResponse(ctx.url("/x"), status_code=303)`.

### 6.9 `ctx.charts`: statistics and charts

For the question *"how much so far this month, against an optional limit, and where is it heading?"*:
spending against a budget, electricity against an allowance, data against a plan, calories against a goal.
The host does the maths and draws the chart (inline SVG, light and dark mode, hover and keyboard readout, a
table view); **your app only supplies the per-day amounts**. The service never touches a database.

```python
from hub.services.chart_units import ChartText, Unit

stats = ctx.charts.stats(daily, limit=300000)                         # numbers only
chart = ctx.charts.progress(daily, limit=300000, unit=Unit.inr())     # numbers + HTML
return ctx.render(request, "usage.html", chart=chart)                 # template: {{ chart.html }}
```

Amounts are **integers in the unit's minor units** (paise for rupees, Wh for kWh, whole counts for notes), so
sums never pick up floating-point error. `daily` is `{day_of_month: amount}` or a list (index 0 = day 1).
The month defaults to the current one (hub timezone); pass `year=`, `month=` and `today=` to choose another.

**`ctx.charts.stats(daily, *, year, month, today, limit, mode, big) -> ProgressStats`**

| Argument | Meaning |
|---|---|
| `daily` | Per-day amounts so far |
| `limit` | The budget / allowance / goal, or `None`. Must be positive |
| `mode` | `"runrate"` (default): the average per day so far × days in the month. `"oneoffs"`: the items in `big` count once as already paid and only the rest is projected forward |
| `big` | `[{"day": 1, "amount": 1500000, "name": "Rent"}, ...]`. Your app decides what is big (for example, a single payment of ₹5,000 or more). They count once in `"oneoffs"` mode, and in every mode they are left out of the day-by-day columns |

`ProgressStats` fields: `spent`, `days_elapsed`, `days_in_period`, `days_left`, `state` (`"current"`, `"past"` or
`"future"`), `mode`, `limit`, `remaining`, `daily`, `cumulative`, `rate` (per-day pace), `projected` (forecast at the
end of the month), `projected_over` (negative = under the limit), `target_daily` (what you can add per day from now
and still finish on the limit), `cut_pct` (how far the pace must fall to do that, `None` if on track),
`recent_pace` and `recent_end` (the same, over the last 7 days), `big` and `big_total`, `big_items` (every item in `big`), `everyday` (per-day amounts without the big items) and `day_target` / `day_target_kind` (the daily target for the columns: `"needed"` = `target_daily` while the month is in progress, `"even"` = the limit split evenly over the month for a finished one, `None` without a limit). Method
`stats.crossing("month" | "recent")` returns the date the total reaches the limit (or the date it already did),
or `None`.

**`ctx.charts.progress(daily, *, ..., unit, view, links, text, columns) -> ProgressChart`** takes the same arguments plus:

| Argument | Meaning |
|---|---|
| `unit` | How amounts are written. `Unit.inr()` (default: ₹, paise, Indian grouping, `₹1.7L`), or `Unit.plain("kWh", minor=1000, decimals=1)`, `Unit.plain("notes")`, `Unit.plain("GB", prefix=False)` |
| `view` | `"climb"` (default: the total rises toward the limit) or `"burn"` (what is left falls to zero; needs a `limit`, otherwise it falls back to climb) |
| `links` | `{"climb": url, "burn": url}` shows a Climb-up / Burn-down toggle. Build the URLs yourself (they are your app's routes); omit for a chart with no toggle |
| `columns` | `True` (default) adds a second pane under the line: a column for each day's everyday spend (big items left out, shown as diamonds) against a horizontal line at the daily target, with the over-target part of a column in red. It shares the day axis and the hover readout. `False` draws the line chart only |
| `text` | `ChartText(title=, noun=, total_name=, left_name=, big_name=, activity=, day_name=, target_name=)` to name things in your domain, e.g. `ChartText(title="Power this month", noun="allowance", total_name="Used so far", left_name="Allowance left", day_name="Power per day", target_name="Daily allowance")` |

It returns a `ProgressChart` with `.html` (put it in a template as `{{ chart.html }}`: it is already safe to render
and escapes any text you passed), `.stats` (the `ProgressStats`), `.view` (as drawn) and `.empty`.

The chart shows the actual line, forecast lines at this month's pace and the last 7 days' pace, the limit, and a
"needed to finish within the limit" line, with a plain-language summary above it ("At this month's pace you'll
reach your ₹40,000 budget on 9 Oct and end at ₹1.7L"). Past months show the final result only; future months and
months with no activity show an empty state.

Notes:
- Switching views or modes is your app's job and needs no JavaScript: link back to the same route with a query
  parameter and pass the choice in. Validate untrusted input with `from hub.services.charts import parse_mode`
  (it returns the value if valid, otherwise the default).
- The chart's CSS and JavaScript are loaded by every hub page (`/static/charts.css`, `/static/charts.js`), so your
  template needs nothing extra.
- Use `stats()` alone when you only need the numbers (a "projected: ..." card, an alert when the limit will be hit).
- Periods are calendar months for now. For a billing cycle that does not start on the 1st, shift the dates into a
  month before passing them in.
- Worked examples: `apps/hello` (notes per day against a goal, the smallest use) and `apps/finance` (spending
  against a budget, both views and both forecast modes).

## 7. UI: templates, components, HTMX

### 7.1 Page skeleton

```jinja
{% extends "base.html" %}
{% import "components.html" as ui %}
{% block title %}Plants · Home Hub{% endblock %}   {# optional; default is "<app name> · Home Hub" #}
{% block head %}<link rel="stylesheet" href="{{ app_static('plants.css') }}">{% endblock %}  {# optional #}
{% block subnav %}{% endblock %}                   {# optional tab bar, see ui.subnav #}
{% block content %}
  ...
{% endblock %}
```

`base.html` provides the top bar (hub link, app icons, Connections, Admin, Log
out), loads `hub.css` and `htmx.min.js`, and wraps `content` in a centred
`.container`.

For several pages with tabs, make a small base template of your own (pattern
from `apps/finance/templates/finance_base.html`):

```jinja
{# templates/plants_base.html #}
{% extends "base.html" %}
{% import "components.html" as ui %}
{% block subnav %}
{{ ui.subnav([(app_url('/'), "Plants"), (app_url('/history'), "History")], request.url.path) }}
{% endblock %}
```

### 7.2 Components (`components.html`)

Import them with `{% import "components.html" as ui %}`. Tones are `good`,
`warn`, `bad`, `info` and `neutral`.

| Macro | Use |
|---|---|
| `ui.stat_card(label, value, sub=None, tone=None)` | A number tile. `tone` colours the value (`good`/`bad`/`warn`) |
| `ui.stats()` | Responsive grid for stat cards: `{% call ui.stats() %}…{% endcall %}` |
| `ui.card(title=None, actions=None)` | A panel: `{% call ui.card("Title") %}…{% endcall %}` |
| `ui.bars(items, empty="Nothing yet.")` | Horizontal bar list. `items` = list of `(label, number, display_text)`; scaled to the max |
| `ui.meter(value, total, label=None)` | Progress bar; turns red over 100% |
| `ui.badge(text, tone="neutral")` | Small pill |
| `ui.empty(text)` | Centred "nothing here" text |
| `ui.flash(text, tone="info")` | Banner; renders nothing if `text` is falsy |
| `ui.job_status(run)` | Badge for a `ctx.scheduler.last_run()` dict (or `None`) |
| `ui.subnav(links, current)` | Tab bar. `links` = list of `(href, label)`; pass `request.url.path` as `current` |

```jinja
{% call ui.stats() %}
  {{ ui.stat_card("Watered this week", 5, sub="of 7 plants") }}
  {{ ui.stat_card("Overdue", 2, tone="bad") }}
{% endcall %}

<div class="columns">
  {% call ui.card("By room") %}
    {{ ui.bars([("Balcony", 4, "4 plants"), ("Kitchen", 2, "2 plants")]) }}
  {% endcall %}
  {% call ui.card("Water budget") %}
    {{ ui.meter(18, 25, label="18 of 25 litres") }}
  {% endcall %}
</div>
```

**Gotcha:** imported macros don't see the page context, including
`request`, `app_url` and your variables. Pass everything a macro needs as
arguments. That is why `subnav` takes `current`.

### 7.3 CSS classes

Use these classes from `hub/static/hub.css` before writing your own CSS. They
handle light and dark mode and phone width.

| Class | Effect |
|---|---|
| `.columns` | Auto-fit grid of cards (two or more columns on wide screens, one on phones) |
| `.row` | Flex row with gap and wrapping, for inline forms |
| `.muted` | Secondary text |
| `table` / `td.num` / `th.num` | Styled tables; `.num` right-aligns with tabular numbers |
| `button`, `.button` | Primary button (also for `<a>`) |
| `.secondary`, `.small`, `.link` | Button variants |
| `form.inline` | Inline form |
| `pre.trace` | Wrapped monospace block (logs, raw text) |
| `.htmx-indicator` | Hidden until its HTMX request is in flight |

If you need more, put a CSS file in `static/` and include it via
`{% block head %}`. Use the hub's CSS variables (`var(--accent)`,
`var(--muted)`, `var(--border)`, `var(--good)`, `var(--bad)`, …) so dark mode
keeps working.

### 7.4 HTMX patterns

HTMX is loaded on every page. Requests from HTMX carry an `HX-Request`
header.

**Partial or full page from the same endpoint** (works without JavaScript
too):

```python
@router.post("/plants", response_class=HTMLResponse)
def add_plant(request: Request, name: str = Form(...)):
    ...
    if request.headers.get("hx-request"):
        return ctx.render(request, "_plant_list.html", plants=load())
    return RedirectResponse(ctx.url("/"), status_code=303)
```

```jinja
<form method="post" action="{{ app_url('/plants') }}"
      hx-post="{{ app_url('/plants') }}" hx-target="#plants" hx-on::after-request="this.reset()">
```

**Inline edit of a table row:** see `apps/finance/templates/_txn_row.html`.
It uses `hx-trigger="change"` and `hx-swap="outerHTML"` on the `<tr>`.

**A "Run now" button with live status** (pattern from `apps/finance`):

```python
@router.post("/sync", response_class=HTMLResponse)
def sync_now(request: Request):
    ctx.scheduler.run_now("sync")            # is_running() is True as soon as this returns
    return sync_status(request, watch=True)

@router.get("/sync/status", response_class=HTMLResponse)
def sync_status(request: Request, watch: bool = False):
    running = ctx.scheduler.is_running("sync")
    headers = {"HX-Refresh": "true"} if watch and not running else None   # finished: reload page
    return ctx.render(request, "_sync_status.html", headers=headers, watch=watch,
                      running=running, run=ctx.scheduler.last_run("sync"))
```

```jinja
{# _sync_status.html #}
{% import "components.html" as ui %}
<div id="sync-status"
  {% if running %}hx-get="{{ app_url('/sync/status') }}?watch=1" hx-trigger="every 2s" hx-swap="outerHTML"{% endif %}>
  <button hx-post="{{ app_url('/sync') }}" hx-target="#sync-status" hx-swap="outerHTML" {{ "disabled" if running }}>
    {{ "Syncing…" if running else "Sync now" }}
  </button>
  {{ ui.job_status(run) }}
</div>
```

**Slow AI call with a spinner:**

```jinja
<button hx-post="{{ app_url('/summarise') }}" hx-target="#summary" hx-indicator="#spin">Summarise</button>
<span id="spin" class="htmx-indicator muted">thinking…</span>
<div id="summary"></div>
```

If a session expires, the host answers HTMX requests with `401` plus
`HX-Redirect: /login…`, so the browser goes to the login page instead of
swapping it into a fragment.

## 8. Routes

- Define every route on the `APIRouter` you return. Paths are relative to
  `/apps/<id>`: `@router.get("/")` serves `/apps/<id>/`.
- Use **sync** handlers (`def`). Every host service blocks, and FastAPI runs
  sync handlers in a thread pool.
- Forms: `name: str = Form(...)` for required fields and `Form("")` for
  optional ones. HTML forms send empty strings, so convert them yourself
  (e.g. `int(x) if x else None`).
- After a successful POST, redirect with `RedirectResponse(ctx.url(...),
  status_code=303)` (post/redirect/get).
- Return `HTMLResponse`/`ctx.render` for pages and dicts for JSON.
  `raise HTTPException(404)` works as usual.
- **Auth is already enforced.** Every route needs a logged-in session, so
  don't add your own login. The session cookie is `SameSite=Lax`, which
  blocks cross-site form posts, so there's no CSRF token to handle.
- There is a single user and no per-user data. Don't build user accounts.

## 9. Rules

Breaking these rules breaks isolation or other plugins:

1. **Use only `ctx`.** Import from the host only:
   - `hub.plugin` (`AppContext`, `Manifest`);
   - `hub.services.ai` (exceptions, `Message`);
   - `hub.services.gmail` (`GmailMessage`, `NotConnected`).

   Don't import `hub.main`, `hub.registry` or other service internals.
2. **Stay in your folder.** Never edit `hub/`, another plugin, or shared
   templates and CSS. If the host is missing something, change the host
   deliberately in a separate change and update this guide.
3. **Own your data.** Use only your own DB file and `ctx.kv`. Never open
   another plugin's database or `hub.db`.
4. **No secrets in code.** API keys go in `.env`, and connection details in
   `hub.toml` (`[connections.*]`).
5. **Respect the AI policy.** Use `ctx.ai`; never call provider SDKs
   directly. Calling them directly would bypass policy and usage logging.
6. **Keep `setup()` fast and offline.** Put slow work in scheduled jobs.
7. **Be idempotent.** Jobs can run twice (manual plus scheduled, or after a
   crash). Use unique keys and `INSERT OR IGNORE`.
8. **Fail soft.** Catch `AIError`, `NotConnected` and HTTP errors in request
   handlers and show a message with `ui.flash`. In jobs, raise so the failure
   lands in run history.
9. **No module-level state shared across requests**, except caches you can
   rebuild. The process may restart at any time.
10. **Times**: use `ctx.tz` for "today" and anything user-facing.
11. **Always name the encoding** when reading or writing text files:
    `path.read_text(encoding="utf-8")`, `open(p, encoding="utf-8")`.
    Windows otherwise uses cp1252 and fails on any emoji or `₹`.
    `tests/test_portability.py` enforces this for every plugin.

## 10. Configuration

### `hub.toml` (structure, committed)

```toml
timezone = "Asia/Kolkata"
disabled = []                      # plugin ids to skip without deleting them

[apps.plants]                      # -> ctx.config
reminder_cron = "0 8 * * *"
connections = ["weather"]          # optional allow-list for ctx.http

[apps.plants.ai]                   # -> enforced AI policy (optional)
allowed_providers = ["lmstudio", "ollama"]
default_alias = "local"

[connections.weather]              # -> ctx.http.client("weather")
base_url = "https://api.openweathermap.org/data/2.5"
auth = { type = "query", name = "appid", env = "OPENWEATHER_API_KEY" }
headers = { Accept = "application/json" }
timeout = 20
```

These `auth` types are available for `[connections.*]`:

| `type` | Sends | Fields |
|---|---|---|
| `bearer` | `Authorization: Bearer <secret>` | `env` |
| `header` | `<name>: <secret>` | `name`, `env` |
| `query` | `?<name>=<secret>` on every request | `name`, `env` |
| `basic` | HTTP basic auth | `env` (user), `password_env` |

`env` names an entry in `.env`. You can use `value = "..."` instead of `env`
for non-secret values.

### `.env` (secrets, never committed)

```
OPENWEATHER_API_KEY=...
```

Document any new keys your plugin needs in `.env.example` and in your
plugin's docstring.

## 11. Testing a plugin

Tests live in `tests/`. Run them with `uv run pytest`. `tests/conftest.py`
provides the following:

| Fixture / helper | Purpose |
|---|---|
| `hub_app(apps_dir=None, toml=None, **settings)` | Builds a full hub app on a temporary data dir. `apps_dir` defaults to the repo's `apps/` |
| `client` | A logged-in `TestClient` for the default hub |
| `login(TestClient)` | Logs a client in (password `pw`) |
| `make_config(tmp_path, apps_dir, toml, **settings)` | A `Config` without touching your real `.env`/`data/` |
| `FakeGmail` | Stand-in for `ctx.gmail`: `.add(id, body, when, subject=...)`, `.connected` |
| `fixture_email(name)` | Reads `tests/fixtures/finance/<name>` as (subject, body) |

`tests/test_ai.py` also has `FakeProvider(name, outputs=[...])`, a stand-in AI
provider that records calls. `extract()` returns the queued outputs in order;
set `.texts = [...]` to script `complete()` replies too (an empty list makes it
raise, to test an unreachable model), and `.temps` records the temperature of
each `complete()` call.

The scheduler isn't started in tests (the `TestClient` isn't used as a context
manager). Call your job directly, or use `ctx.scheduler.run_now(job,
wait=True)`.

```python
# tests/test_plants.py
from fastapi.testclient import TestClient

from tests.conftest import login
from tests.test_ai import FakeProvider


def test_add_and_list(hub_app):
    app = hub_app()
    entry = app.state.hub.registry.apps["plants"]
    assert entry.status == "loaded", entry.error

    client = login(TestClient(app))
    client.post("/apps/plants/plants", data={"name": "Fern"})
    assert "Fern" in client.get("/apps/plants/").text

    ctx = entry.ctx                                # the real AppContext
    with ctx.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0] == 1


def test_ai_feature_without_network(hub_app):
    app = hub_app()
    hub = app.state.hub
    hub.ai.providers = {"anthropic": FakeProvider("anthropic"), "ollama": FakeProvider("ollama")}
    ctx = hub.registry.apps["plants"].ctx
    assert ctx.ai.complete("hi").text == "hi"      # FakeProvider.complete returns "hi"
```

- Pass `toml={...}` to `hub_app` to test your `[apps.<id>]` config or AI
  policy. Start from `tests.conftest.BASE_TOML` and extend it.
- For a plugin that uses Gmail, replace the client:
  `ctx._gmail = FakeGmail()`.
- Test pure logic (parsers, maths) as plain functions without the hub. See
  `tests/test_finance_parsers.py`.
- Keep fixtures free of personal data. Redact real samples before
  committing them.

## 12. Checklist before shipping

- [ ] The folder name and `manifest.id` match and follow `^[a-z][a-z0-9_]*$`.
- [ ] Migrations are numbered, and none that has already run was edited.
- [ ] All plugin-internal imports are relative.
- [ ] `requires` lists every connection you use. The UI copes with it being
      not connected yet.
- [ ] AI calls catch `AIError`. Private data has a `[apps.<id>.ai]` policy.
- [ ] Jobs are idempotent and raise on failure.
- [ ] Every link and form uses `app_url()` / `ctx.url()`.
- [ ] Pages look right at phone width and in dark mode.
- [ ] New config keys are documented, and new secrets are added to
      `.env.example`.
- [ ] Any extra pip packages are pinned in `apps/<id>/requirements.txt`.
- [ ] Every `open()` / `read_text()` / `write_text()` passes `encoding="utf-8"`.
- [ ] `uv run pytest` passes. After a restart, `/admin` shows the plugin as
      **loaded** and its jobs are listed.

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/admin` shows **failed** with `manifest id 'x' must match folder 'y'` | Make `manifest.id` equal the folder name |
| `folder name 'My-App' must match ^[a-z][a-z0-9_]*$` | Rename the folder (lowercase, digits, `_`) |
| `module must define manifest = Manifest(...)` / `setup(ctx) -> APIRouter` | Missing or misnamed module-level `manifest` or `setup` |
| `setup(ctx) must return a fastapi.APIRouter` | `return router` at the end of `setup` |
| `unknown requirement(s) [...]` | Only values from [4. The manifest](#requires) are allowed |
| `ModuleNotFoundError: No module named 'apps'` (or a double-loaded module) | Use relative imports inside the plugin |
| Migration error at startup | Fix the SQL. Nothing from a failed file was applied, so restart after fixing |
| `/apps/<id>/` returns 404 | The folder has no `__init__.py`, the name starts with `_` or `.`, or the hub hasn't been restarted |
| `/apps/<id>/` returns 503 | The plugin failed to load. See the traceback on that page or `/admin` |
| `AIPolicyError` | The model resolves to a provider outside `[apps.<id>.ai] allowed_providers` |
| `ProviderUnavailable` | Set the provider's `<NAME>_API_KEY` in `.env` (or point the alias at a configured provider) |
| `NotConnected` | Do the one-time connect at `http://localhost:8000/connections` **on the host PC** |
| Jinja `'request' is undefined` inside a macro | Imported macros don't see page context; pass it as an argument |
| A job "ran" but nothing happened | Check its row on `/admin` (status, error). Did it raise? Is it registered in `setup()`? |
| "Today" is off by a day | Use `datetime.now(ctx.tz)`, not `date.today()` |

## 14. Worked examples in this repo

**`apps/hello`**: the smallest complete plugin, and the one to copy. It
shows:

- a migration (`migrations/001_notes.sql`);
- HTMX partials (`templates/_notes.html`);
- `ctx.ai.complete` with `AIError` handling;
- an interval job writing to `ctx.kv`;
- `ctx.ai.available()` gating the UI.

**`apps/finance`**: a full plugin:

| File | Shows |
|---|---|
| `__init__.py` | `requires=["gmail"]`, custom Jinja filter, cron job from `ctx.config` |
| `migrations/001_init.sql` | Several tables, foreign keys, seed rows |
| `sync.py` | Idempotent Gmail import, per-item error isolation, a job that raises on failure |
| `extract.py` | `ctx.ai.extract` with a Pydantic schema and validator, local-only AI policy |
| `learn.py` | Rules in the DB with a score, a retrying `ctx.ai.complete` loop that varies `temperature`, jobs started on demand with `ctx.scheduler.run_now` (see `docs/finance-parsing.md`) |
| `routes.py` | Many pages, forms, HTMX partials, the "Sync now" pattern |
| `templates/finance_base.html` | Shared tab bar via `ui.subnav` |
| `templates/_txn_row.html` | Inline row editing |
| `tests/test_finance.py` | Testing with `hub_app`, `FakeGmail` and `FakeProvider` |
