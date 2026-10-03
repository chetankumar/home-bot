# Home Hub

One always-on host on the home LAN for many small home-automation apps. The
host owns the shared plumbing (AI providers, SQLite, outbound API connections,
OAuth, scheduling, login, UI shell). Each app is a folder under `apps/`,
mounted at `/apps/<id>/`. To add an app you add a folder; the host and the
other apps stay untouched.

Two apps ship with it:

- **Hello** (`apps/hello`): the reference app and the template to copy.
- **Finance** (`apps/finance`): reads HDFC transaction alerts from Gmail,
  tags spends by recipient, and shows month-to-date burn against a budget.
  Its AI use is pinned to a local Ollama model.

Stack: Python 3.12, FastAPI, Jinja2 + HTMX (vendored, no Node), SQLite,
APScheduler, httpx, and the `anthropic` / `openai` / `google-genai` SDKs.

## Quick start

**Full instructions: [docs/installation.md](docs/installation.md).** That
guide covers a fresh conda env, LAN access, installing plugins, how
migrations run, upgrades and backups. The short version:

```bash
git clone https://github.com/chetankumar/home-bot.git && cd home-bot
conda create -n home-hub python=3.12 -y && conda activate home-hub
python -m pip install -r requirements.txt
copy .env.example .env      # cp on macOS/Linux; then set HUB_PASSWORD
python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

Or, with [uv](https://docs.astral.sh/uv/): `uv sync`, then
`uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000`.

Open http://localhost:8000 and log in with `HUB_PASSWORD`. From other devices
on the LAN, use `http://<host-ip>:8000`.

Run the tests with `python -m pip install -r requirements-dev.txt`, then
`python -m pytest` (or `uv run pytest`).

### Configuration

| File | Holds |
|---|---|
| `.env` | Secrets: `HUB_PASSWORD`, `HUB_SECRET_KEY`, provider API keys, `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. Never committed. |
| `hub.toml` | Structure: timezone, AI providers and aliases, outbound connections, per-app settings and AI policy, disabled apps. |
| `data/` | Runtime state (gitignored): `hub.db` (settings, AI usage, encrypted OAuth tokens, job history) and `apps/<id>.db` (one file per app). |

`HUB_SECRET_KEY` signs the login cookie and encrypts stored OAuth tokens. If
you leave it blank, one is generated into `data/.secret_key` on first start.
If the key changes, stored tokens can no longer be decrypted and you have to
reconnect Google.

## Pages

| Path | What |
|---|---|
| `/` | Launcher. Failed apps and missing connections are flagged here. |
| `/apps/<id>/` | Each app. |
| `/connections` | Connect or disconnect Google. |
| `/admin` | Loaded and failed apps (with tracebacks), scheduled jobs with last-run status and a **Run now** button, AI usage per app and provider, connection status. |

Everything except `/login`, `/static` and `/healthz` requires login.

## Building a plugin

**See the [plugin guide](docs/plugin-guide.md).** It is the full reference
for anyone, human or AI agent, building a new app. It covers:

- the folder layout and manifest;
- every host service on `ctx` (database, AI, HTTP, Gmail, scheduler, kv,
  templates);
- the shared UI components;
- configuration, testing, rules, and troubleshooting.

To **install** an existing plugin, see
[docs/installation.md, Part 2](docs/installation.md#part-2-install-a-plugin).
The hub applies each plugin's database migrations automatically at startup.

To build one, in short:

1. Copy `apps/hello` to `apps/<id>`, where `<id>` is lowercase letters,
   digits or `_`.
2. Set `manifest.id` to the folder name.
3. Restart the hub. The app appears at `/apps/<id>/` with its own database
   at `data/apps/<id>.db`.

If an app fails to load, only that app is marked failed. The traceback is on
`/admin`, and everything else keeps running. To switch an app off without
deleting it, add it to `disabled = [...]` in `hub.toml`.

## AI providers

Models are addressed as `provider:model` or by an alias from `hub.toml`:

```toml
[ai.aliases]
default = "anthropic:claude-opus-5-5"
fast    = "anthropic:claude-haiku-4-5-20251001"
local   = "ollama:llama3.1:8b"
```

Three adapters cover the five providers. `anthropic` uses the Messages API.
`openai_compat` covers OpenAI, OpenRouter, Ollama, and any other `base_url`.
`gemini` uses `google-genai`. Each key is read from `<NAME>_API_KEY` in `.env`
(override with `api_key_env`). A provider without a key is reported as
unavailable rather than failing startup.

`extract()` uses each provider's native structured output (forced tool use for
Anthropic, a JSON-schema `response_format` for OpenAI-compatible servers
including Ollama, and a response schema for Gemini). It validates the result
into your Pydantic model and retries once with the validation error if that
fails.

**Per-app policy** is enforced by the host, not by the app:

```toml
[apps.finance.ai]
allowed_providers = ["ollama"]
default_alias = "local"
```

A call to any other provider raises `AIPolicyError` before any network
request. `/admin` shows usage per app and provider, so you can confirm that
Finance only ever used Ollama.

### Ollama

Install Ollama, pull a model (for example `ollama pull llama3.1:8b`), and set
the `local` alias in `hub.toml` to `ollama:<that model>`. Finance only uses the
model as a fallback for alerts the regex parsers don't recognise, and to
suggest categories for merchant names. If Ollama is down, sync still works and
the unrecognised emails wait on the Review page.

## Google Cloud OAuth setup (for Gmail)

This is a one-time setup. The hub only asks for `gmail.readonly`.

1. Go to https://console.cloud.google.com and create a project (e.g. "Home Hub").
2. **APIs & Services → Library**: search for "Gmail API" and click **Enable**.
3. **Google Auth Platform** (called "OAuth consent screen" in older consoles) → **Get started**:
   - App name "Home Hub", with your email as the support email.
   - Audience: **External**.
   - Contact email: yours. Agree to the policy and click **Create**.
4. **Data Access → Add or remove scopes**: add
   `https://www.googleapis.com/auth/gmail.readonly` and save.
5. **Audience → Publish app**, so the status reads **In production**.
   *This matters:* in "Testing" status Google expires refresh tokens after 7
   days, so the daily sync would stop working. Leaving the app unverified is
   fine for personal use. During consent you'll see "Google hasn't verified
   this app"; click **Advanced → Go to Home Hub**.
6. **Clients → Create client**:
   - Application type: **Web application**.
   - Authorized redirect URIs: `http://localhost:8000/connections/google/callback`
   - Click **Create**, then copy the client ID and secret into `.env` as
     `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Restart the hub.
7. **On the host PC**, open http://localhost:8000/connections, log in, and
   click **Connect Google**.

Google only accepts `https` or `localhost` redirect URIs, never a LAN IP. That
is why step 7 has to happen in a browser on the host itself; the page refuses
to start the flow from another device and says so. After that one-time
consent, Finance works from any device on the LAN. If you serve the hub on a
different port or host name, set `HUB_BASE_URL` and register the matching
redirect URI.

## Finance

1. Connect Google (see above).
2. Go to **Finance → Settings**. Set a monthly budget and check the sender
   addresses. The defaults are `alerts@hdfcbank.net` and
   `alerts@hdfcbank.bank.in`.
3. Click **Sync now** on the dashboard. The first run backfills from the 1st of
   the current month (IST). Later runs start from the newest stored email minus
   a day of overlap. A daily sync also runs at 07:15; to change it, set
   `sync_cron` under `[apps.finance]` in `hub.toml`.
4. **Recipients** lists the counterparties you've paid, biggest spend first.
   Name one and pick a category, and every past and future transaction with
   that UPI id or merchant name is tagged. To attach another UPI id to someone
   you've already named, reuse their name. A category set by hand on a single
   transaction survives later re-tagging.
5. **Review** shows emails that nothing could parse. You can enter the
   transaction by hand or ignore the email. After parsers improve,
   **Re-parse** runs them again over unparsed and auto-ignored mail; emails
   you ignored by hand are left alone.

How the numbers work:

- **Spend** is debits in categories that count as spend. Uncategorised
  debits count. **Transfers** doesn't count, so tag card-bill payments and
  moves between your own accounts as Transfers.
- **Daily burn** is spend ÷ days elapsed.
- **Projected** is daily burn × days in the month.

Once a full past month has been synced, Settings shows the trailing average
as a budget hint.

Raw email bodies are stored. HDFC's wording varies, so expect the first real
sync to put some emails on the Review page. The parsers in
`apps/finance/parsers.py` can then be tightened against them, with redacted
samples added under `tests/fixtures/finance/`.

## Layout

```
hub/            host: main.py (create_app), config, auth, plugin contract, registry
  services/     db, kv, http, oauth, gmail, scheduler, ai/ (service + 3 adapters)
  templates/    base, launcher, login, admin, connections, components.html
  static/       hub.css, htmx.min.js
  migrations/   hub.db schema
apps/hello/     reference app
apps/finance/   Finance app
tests/          host + finance tests, fixture apps, redacted email fixtures
docs/           installation.md (install, plugins, migrations, upgrades);
                plugin-guide.md (the reference for building plugins)
```

## Not included yet

Running as a Windows service on boot, HTTPS, multiple users, hot-reloading
apps without a restart, running apps in separate processes, banks other than
HDFC, and SMS or statement-PDF ingestion.
