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
  Its AI use is pinned to a local model server (LM Studio or Ollama).

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
local   = "lmstudio:qwen2.5-7b-instruct"
```

Three adapters cover every provider. `anthropic` uses the Messages API.
`openai_compat` covers OpenAI, OpenRouter, the local servers LM Studio and
Ollama, and any other OpenAI-compatible `base_url`.
`gemini` uses `google-genai`. Each key is read from `<NAME>_API_KEY` in `.env`
(override with `api_key_env`). A provider without a key is reported as
unavailable rather than failing startup.

`extract()` uses each provider's native structured output (forced tool use for
Anthropic, a JSON-schema `response_format` for OpenAI-compatible servers
including LM Studio and Ollama, and a response schema for Gemini). It validates the result
into your Pydantic model and retries once with the validation error if that
fails.

**Per-app policy** is enforced by the host, not by the app:

```toml
[apps.finance.ai]
allowed_providers = ["lmstudio", "ollama"]   # local servers only, never cloud
default_alias = "local"
```

A call to any other provider raises `AIPolicyError` before any network
request. `/admin` shows usage per app and provider, so you can confirm that
Finance only ever used a local model.

### Local models (LM Studio or Ollama)

Finance uses the `local` alias in two places:
- as a fallback for alerts the regex parsers don't recognise;
- to suggest categories for merchant names.

If the local server is down, sync still works and the unrecognised emails wait
on the Review page. **Finance → Settings** shows which model `local` resolves
to.

**LM Studio** (the default in `hub.toml`):

1. In LM Studio, download an instruct model. A 7–8B model such as Qwen2.5 7B
   Instruct or Llama 3.1 8B Instruct is plenty for reading bank alerts.
2. Open the **Developer** tab, select the model, and start the server. It
   listens on `http://localhost:1234`.
3. Copy the model's identifier, shown in the Developer tab and listed at
   http://localhost:1234/v1/models. Set it in `hub.toml`:

   ```toml
   local = "lmstudio:<model identifier>"
   ```

4. Make sure the model is available when the daily sync runs at 07:15. Either
   keep LM Studio running with the model loaded, or turn on just-in-time model
   loading in the Developer settings, which loads the model on the first
   request. LM Studio can also start its server at login.

The hub talks to LM Studio through its OpenAI-compatible API, including
JSON-schema structured output, so no extra setup is needed.

**Ollama**: pull a model (`ollama pull llama3.1:8b`) and set
`local = "ollama:llama3.1:8b"`.

**Another local server** (llama.cpp's `llama-server`, vLLM, …): add a block
like the `lmstudio` one in `hub.toml`. Use `type = "openai_compat"`, its
`base_url`, and `key_required = false`. Add its name to
`[apps.finance.ai] allowed_providers`.

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
4. **Describe a spend instead of picking a category.** On **Transactions**,
   type what it was ("weekly vegetables from the market") and click
   **Categorise**. The local model files it under one of your categories, or,
   if none fits, proposes a new one that you confirm with one click. Nothing is
   created without that click. If the recipient has no category yet, it gets
   this one, so future spends from them are filed automatically; a recipient
   that already has a category is never changed. "Pick manually" under each
   row is the fallback for when the model is off or wrong.
5. **Recipients** lists the counterparties you've paid, biggest spend first.
   Name one and say what you buy from them; the same flow picks (or proposes)
   the category, and every past and future transaction with that UPI id or
   merchant name is tagged. To attach another UPI id to someone you've already
   named, reuse their name. A category set by hand on a single transaction
   survives later re-tagging.
6. **Categories** is yours to shape: add, rename, delete, merge two into one,
   and mark which ones count as spending. Each category has a description
   that is shown to the model, so wording it well teaches the model what
   belongs there ("Food: restaurants, takeaway, Swiggy and Zomato").
7. **Amazon orders.** Each sync also reads order emails from
   `auto-confirm@amazon.in` (change the senders in Settings) and keeps the
   items, total and status (placed, shipped, delivered, cancelled) on
   **Orders**. Every order is then matched to the Amazon charge in your bank
   alerts. The strongest signal is **timing**: your bank's alert and Amazon's
   order email reach your inbox within minutes of each other. A match shows
   the items on the transaction and pre-fills the narration, so one click
   files it, and says why it matched ("same amount, 2 min apart"). Matching is
   cautious, strongest first:
   - the **same amount within 30 minutes** (or a unique same-amount pairing
     within 1 day before to 14 days after) is **exact**;
   - when several orders share an amount, the closest in time wins and the
     match is marked **check**;
   - an order with **no total** still matches if exactly one charge lands within
     30 minutes of it and the amount is plausible (within ₹150 or 25% of the
     item prices, to allow for delivery fees and coupons); it is marked **check**
     and says how far the amount is off. If two orders compete for one charge
     it is left for you rather than guessed;
   - an order charged in two or three shipments, or a charge equal to some of
     the items, matches as a group;
   - cancelled orders never match, and a cancellation frees an earlier match;
   - anything else stays unmatched; you can **link** or **unlink** by hand.

   When an email has no total line, the total is estimated from the item
   prices (shown as "≈", and replaced if a later email states the real one).
   **view email** on such an order shows what was stored, in case the layout
   needs a parser tweak. The window is `amazon_match_minutes` (default 30) and
   the amount slack is `amazon_amount_slack` in rupees (default 150), both under
   `[apps.finance]` in `hub.toml`.

   **Older orders need older bank alerts.** The first sync only reads bank
   alerts from the 1st of the current month, so an order from before that has no
   charge to match. In **Settings → Fetch older bank alerts** pick a date and
   click **Fetch & sync**: it reaches back once (alerts already stored are
   skipped), and the Amazon scan reaches back with it.

   Orders have their own **Sync orders** button on the Orders page, with live
   status, so you can refresh them without re-reading your bank alerts. They are
   also scanned by the daily sync and again each evening at 19:45 (set
   `orders_cron` under `[apps.finance]` to change it), so same-day orders and
   shipping updates are picked up. A scan that fails shows its error there and
   in the job history on `/admin`.

   The first run looks back 90 days; set `amazon_backfill_days` under
   `[apps.finance]` in `hub.toml` to change it. Orders from before your synced
   bank history are marked as such rather than as missing.
8. **Review** shows emails that nothing could parse. You can enter the
   transaction by hand or ignore the email. After parsers improve,
   **Re-parse** runs them again over unparsed and auto-ignored mail; emails
   you ignored by hand are left alone. Amazon emails the parser couldn't read
   wait on **Orders**, with their own Re-parse.


How the numbers work:

- **Spend** is debits in categories that count as spend. Uncategorised
  debits count. **Transfers** doesn't count, so tag card-bill payments and
  moves between your own accounts as Transfers.
- **Daily burn** is spend ÷ days elapsed.
- **Projected** is daily burn × days in the month.
- **Target daily burn** (with a budget set) is what you can spend per day for
  the rest of the month to finish exactly on budget: (budget − spent) ÷ days
  left, rounded down. When the projection is over, it shows how far the
  current rate must fall; when you're on track it reads **Daily allowance**;
  when you've already exceeded the budget it says so, since no daily rate can
  recover it.

**The chart** under the dashboard cards grows as you spend. **Climb-up** shows
spend rising through the month toward your budget; **Burn-down** shows the
budget left falling toward zero. Both draw the forecast lines (at this month's
pace, and at the last 7 days' pace), the budget, and the daily pace you need to
finish within it, with a sentence above ("At this month's pace you'll reach
your ₹40,000 budget on 9 Oct…"). Hover (or use the arrow keys) to read any day;
the **Table view** under it has every number.

The **Forecast** control above the cards chooses how the month is projected,
for the chart and the cards together, and is remembered:
- **Run-rate**: the average daily spend so far × days in the month (the default).
- **Big payments once**: a single payment of ₹5,000 or more (set
  `oneoff_threshold` under `[apps.finance]`) counts once as already paid, and
  only the everyday spend is projected. A ₹15,000 rent on the 1st no longer
  gets multiplied across the month.

The chart is a **host service**, so any app can have one: see
[`ctx.charts` in the plugin guide](docs/plugin-guide.md#69-ctxcharts-statistics-and-charts).
The Hello app uses it for notes against a monthly goal.

Once a full past month has been synced, Settings shows the trailing average
as a budget hint.

Raw email bodies are stored. HDFC's and Amazon's wording varies and I have not
seen your emails, so expect the first real sync to leave some on the Review and
Orders pages. For Amazon, the parser reads the order number, items and total from
the plain-text layout, then the subject line, then asks the local model; only the
emails it still can't read wait for you. The parsers in
`apps/finance/parsers.py` and `apps/finance/amazon.py` can then be tightened
against them, with redacted samples added under `tests/fixtures/`.

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
