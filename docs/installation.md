# Installing and running Home Hub

This guide covers:

- installing the hub in a fresh conda environment;
- running it on your home network;
- installing, updating and removing plugins;
- how database migrations work (short answer: **the hub runs them
  automatically at startup; you never run them by hand**).

Building a plugin is covered separately in the
[plugin guide](plugin-guide.md).

- [Part 1: Install the hub](#part-1-install-the-hub)
- [Part 2: Install a plugin](#part-2-install-a-plugin)
- [Part 3: Migrations: who runs them and when](#part-3-migrations-who-runs-them-and-when)
- [Part 4: Upgrade, back up, restore](#part-4-upgrade-back-up-restore)

---

## Part 1: Install the hub

### Prerequisites

- **Miniconda or Anaconda**: https://docs.conda.io/en/latest/miniconda.html.
  On Windows, run the commands below in the **Anaconda Prompt**, or in a
  PowerShell where `conda init powershell` has been run.
- **git**.
- Optional:
  - [LM Studio](https://lmstudio.ai) (or [Ollama](https://ollama.com)) with
    a model downloaded, for local AI (the Finance app uses it; see
    [Local models](../README.md#local-models-lm-studio-or-ollama));
  - a Google Cloud OAuth client, for Gmail (see
    [Google Cloud OAuth setup](../README.md#google-cloud-oauth-setup-for-gmail)).

Pick one always-on PC on your LAN to be the host. The steps are the same on
Windows, macOS and Linux unless marked otherwise.

### 1. Get the code

```bash
git clone https://github.com/chetankumar/home-bot.git
cd home-bot
```

### 2. Create a fresh conda environment

```bash
conda create -n home-hub python=3.12 -y
conda activate home-hub
python -c "import sys; print(sys.executable)"
```

**Check that last line before installing anything.** It must print a path
inside the env:

- Windows: `...\envs\home-hub\python.exe`
- macOS/Linux: `.../envs/home-hub/bin/python`

If it prints anything else (e.g. `...\Python314\python.exe` or
`/usr/bin/python3`), the env isn't active. Packages would then go into your
global Python and can break other projects there. In that case:

- Windows PowerShell: run `conda init powershell` once and open a new
  window, or use the **Anaconda Prompt**.
- macOS/Linux: run `conda init` and restart your shell.

Python 3.12 or newer is required. Do this check in **every new terminal**
before running the hub or `pip`; the env is only active in the window where
you ran `conda activate`.

### 3. Install the dependencies

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` pins exact versions, generated from `uv.lock`, so you get
the same packages the hub was tested with. To run the test suite as well,
install the dev requirements instead:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest            # all tests should pass
```

Always use **`python -m pip`**, not bare `pip`. It guarantees the packages go
into the same Python that runs the hub, because a bare `pip` on your PATH can
belong to a different installation.

Don't use `conda install <package>` for these; mixing conda and pip for the
same packages causes version conflicts.

If pip prints "dependency conflicts" mentioning packages this project doesn't
use (langchain, opentelemetry, …), **stop**. You're installing into the wrong
Python. Activate the env and repeat the check above.

### 4. Configure

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

Edit `.env`:

| Key | Set it to |
|---|---|
| `HUB_PASSWORD` | The login password. **Required.** Nobody can log in without it. |
| `HUB_SECRET_KEY` | A long random string: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. If you leave it blank, one is generated into `data/.secret_key` on first start. Don't change it later: it encrypts stored Google tokens. |
| `HUB_BASE_URL` | Leave as `http://localhost:8000` unless you change the port. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY` | Only the providers you use. Providers without a key are just unavailable. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Only if you use Gmail-based plugins. |

Then check `hub.toml`:

- `timezone` (default `Asia/Kolkata`);
- the `local` alias under `[ai.aliases]`, which must name a model your local
  server has: `lmstudio:<model identifier>` (from LM Studio's Developer tab
  or http://localhost:1234/v1/models), or `ollama:<model>` (from
  `ollama list`).

  Finance → Settings shows what `local` resolves to.

`.env` and `data/` are gitignored. Never commit them.

### 5. Run it

From the repo folder, with the env active:

```bash
python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

On first start the hub creates `data/`, including `hub.db` and one database
per app under `data/apps/`, and applies every migration (see
[Part 3](#part-3-migrations-who-runs-them-and-when)). The console shows lines
like this:

```
INFO hub: hub.db: applied migrations 001_init.sql
INFO hub.registry: app finance: applied migrations 001_init.sql
INFO hub.registry: loaded app finance
INFO hub.registry: loaded app hello
```

Open http://localhost:8000 and log in. Then go to **/admin**, where every app
should show **loaded**, with its database and latest migration.

Stop the hub with `Ctrl+C`.

### 6. Reach it from other devices

The hub listens on all interfaces (`--host 0.0.0.0`). From a phone or laptop
on the same Wi-Fi, open `http://<host-ip>:8000`. Find the host's IP with
`ipconfig` (Windows) or `ip addr` / `ifconfig` (macOS/Linux).

On Windows, allow the port through the firewall for private networks once
(in an Administrator prompt):

```powershell
netsh advfirewall firewall add rule name="Home Hub" dir=in action=allow protocol=TCP localport=8000 profile=private
```

Connecting Google is the one thing that must be done **on the host PC
itself**, at http://localhost:8000/connections, because Google only
redirects back to `localhost`. After that, everything works from any device.

### 7. Optional: start it with one command

Make a start script that doesn't need the env activated first:

```bat
:: start-hub.bat (Windows). Put it in the repo folder
cd /d %~dp0
conda run --no-capture-output -n home-hub python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

```bash
# start-hub.sh (macOS / Linux)
cd "$(dirname "$0")"
conda run --no-capture-output -n home-hub python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

You can point Windows Task Scheduler ("At log on") or a systemd user service
at that script. Running it as a proper service on boot is not covered yet.

### Alternative: uv instead of conda

If you'd rather not use conda, [uv](https://docs.astral.sh/uv/) creates and
manages a `.venv` from `uv.lock`:

```bash
uv sync
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

---

## Part 2: Install a plugin

A plugin is a folder under `apps/`. Installing one is the same whether you
wrote it, got it from someone else, or generated it with an AI agent.

### 1. Put the folder in `apps/`

```
home-bot/
  apps/
    hello/
    finance/
    plants/              <- the new plugin
      __init__.py        <- must be directly inside, not apps/plants/plants/__init__.py
      migrations/
      templates/
```

- **Copy** the folder, unzip it, or clone a plugin repo into `apps/<id>`.
- The **folder name must equal the plugin id** (the `id=` in its
  `Manifest`), using lowercase letters, digits and `_`. If they differ, the
  plugin fails to load with `manifest id 'x' must match folder 'y'`.

### 2. Install the plugin's Python packages, if any

Most plugins need nothing beyond what the hub already has: FastAPI, httpx,
Pydantic, the AI SDKs, and so on. If a plugin needs more, it should list
those packages in `apps/<id>/requirements.txt`. Install them into the same
env:

```bash
conda activate home-hub
python -m pip install -r apps/plants/requirements.txt
```

If you skip this step, the plugin fails to load with a
`ModuleNotFoundError`, shown on `/admin`. The rest of the hub is unaffected.

### 3. Add its configuration

Check the plugin's docstring or README for what it needs. It may need any of
these:

- **Settings** in `hub.toml` under `[apps.<id>]`:

  ```toml
  [apps.plants]
  reminder_cron = "0 8 * * *"
  ```

- **An AI policy** under `[apps.<id>.ai]`, if you want to restrict which
  providers it may use (recommended for anything handling private data):

  ```toml
  [apps.plants.ai]
  allowed_providers = ["lmstudio", "ollama"]   # local model servers only
  default_alias = "local"
  ```

- **An outbound API** under `[connections.<name>]`, plus the API key in
  `.env`.
- **A connection** such as Gmail (`requires=["gmail"]`). After the restart,
  connect it once at http://localhost:8000/connections on the host PC.

### 4. Restart the hub

Stop the hub (`Ctrl+C`) and start it again. Plugins are only discovered at
startup.

On startup the hub:

1. imports the plugin;
2. creates `data/apps/<id>.db` and **runs the plugin's migrations**;
3. calls its `setup()`;
4. mounts it at `/apps/<id>/`.

### 5. Check it

Open **/admin**:

| You see | Meaning |
|---|---|
| **loaded**, with "N migrations, latest `00N_….sql`" | Installed. Open it from the launcher or `/apps/<id>/` |
| **failed**, with a traceback | Read the last line of the traceback. Common causes: folder name ≠ id, missing package (step 2), a migration error. Fix it and restart |
| A yellow requirement badge (e.g. `gmail`) | It loaded, but needs a connection: go to `/connections` |

Any scheduled jobs the plugin registered appear under **Scheduled jobs** on
`/admin`, with a **Run now** button.

### Update a plugin

1. Stop the hub.
2. Back up `data/apps/<id>.db` (see [Part 4](#back-up)).
3. Replace the plugin folder with the new version (or `git pull` inside it).
4. Install any new packages: `python -m pip install -r apps/<id>/requirements.txt`.
5. Start the hub. New migrations in the update are applied automatically,
   and your existing data is kept.

### Disable a plugin (keep it and its data)

Add its id to `disabled` in `hub.toml`, then restart:

```toml
disabled = ["plants"]
```

A disabled plugin isn't imported at all. Its migrations don't run and its
jobs aren't scheduled. Remove it from the list and restart to bring it back.

### Remove a plugin

1. Stop the hub.
2. Delete `apps/<id>/`.
3. Optionally delete its data: `data/apps/<id>.db` (plus `-wal` / `-shm`
   files if present).
4. Optionally remove its `[apps.<id>]` and `[connections.*]` entries from
   `hub.toml`.
5. Start the hub.

Its small settings rows in `hub.db` are left behind. They're harmless and
are reused if you reinstall it.

---

## Part 3: Migrations: who runs them and when

**The hub runs them, automatically, every time it starts.** There is no
migrate command, and you never run SQL by hand.

### What happens at startup

1. The hub's own schema: files in `hub/migrations/` are applied to
   `data/hub.db`.
2. For **each enabled plugin**, before its `setup()` is called, files in
   `apps/<id>/migrations/` are applied to that plugin's own
   `data/apps/<id>.db`.

For each database, the hub:

- reads the `_migrations` table inside that database file, which lists every
  migration already applied (`name`, `applied_at`);
- finds files named `NNN_name.sql` that are not in that table;
- runs them **in numeric order** (`2_…` before `10_…`), each **inside a
  transaction**, and records each one in `_migrations`.

Already-applied files are skipped, so starting the hub repeatedly is safe.

### When a migration fails

- The failing file is rolled back completely, so nothing from it is
  half-applied, and it isn't recorded.
- **Only that plugin** fails to load. `/admin` shows the SQL error, and the
  hub and every other plugin keep running.
- Fix the SQL file (or get a fixed plugin version) and restart. The hub
  retries it.

A failure in the hub's own migrations stops the hub from starting. That only
happens if the hub code itself is broken.

### How to see what's been applied

- **/admin**: the "Data / schema" column shows each plugin's migration count
  and latest file.
- **Console**: the hub logs `app <id>: applied migrations …` whenever it
  applies something new.
- **Directly**: `python -c "import sqlite3; print(sqlite3.connect('data/apps/finance.db').execute('select * from _migrations').fetchall())"`.

### Things to know

- **There are no down-migrations.** To undo a schema change, restore the
  backup you took before upgrading (Part 4).
- **Never edit a migration that has already been applied.** The hub won't
  re-run it, so your database and the file would silently disagree. Plugin
  authors add a new numbered file instead (see the
  [plugin guide](plugin-guide.md#61-ctxdb-your-sqlite-database)).
- Disabled plugins' migrations don't run until you enable them.
- Each plugin's migrations only touch its own database file. One plugin
  can't change another's tables.

---

## Part 4: Upgrade, back up, restore

### Back up

Everything you'd lose lives in two places: **`data/`** and your **`.env`**.

1. **Stop the hub first.** SQLite runs in WAL mode, so while the hub is
   running, recent changes may sit in `*.db-wal` files. Stopping it folds
   them into the `.db` files.
2. Copy the whole `data/` folder and `.env` somewhere safe:

   ```bash
   # Windows (PowerShell)
   Copy-Item -Recurse data ..\home-hub-backup-$(Get-Date -Format yyyyMMdd)\data
   Copy-Item .env ..\home-hub-backup-$(Get-Date -Format yyyyMMdd)\

   # macOS / Linux
   mkdir -p ../home-hub-backup-$(date +%Y%m%d) && cp -r data .env ../home-hub-backup-$(date +%Y%m%d)/
   ```

The backup contains your encrypted Google token. It's only usable with the
same `HUB_SECRET_KEY`, which is why `.env` goes with it.

### Upgrade the hub

```bash
# 1. stop the hub (Ctrl+C), then back up data/ and .env (above)
git pull
conda activate home-hub
python -m pip install -r requirements.txt      # picks up new or upgraded packages
python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

New hub and plugin migrations are applied on that start. Then check
`/admin`.

### Restore

Stop the hub, replace `data/` (and `.env`) with the backup copy, check out
the matching code version (`git checkout <commit>`), and start the hub.

### Rebuild the conda env from scratch

```bash
conda deactivate
conda env remove -n home-hub
conda create -n home-hub python=3.12 -y
conda activate home-hub
python -m pip install -r requirements.txt
python -m pip install -r apps/<id>/requirements.txt   # for each plugin that has one
```

Your data is untouched. It lives in `data/`, not in the env.

### For maintainers: keeping requirements.txt in sync

`pyproject.toml` and `uv.lock` are the source of truth. After changing
dependencies, regenerate the pinned files:

```bash
uv lock
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
uv export --only-group dev --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt
```

Then put `-r requirements.txt` as the first line of `requirements-dev.txt`.
`tests/test_docs.py` fails if a dependency in `pyproject.toml` is missing
from `requirements.txt`.
