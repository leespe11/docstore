# Document Storage — local OCR + RAG document assistant

Self-hosted app to ingest scanned documents / files, OCR + index them, and
chat with a local LLM that can look things up, do math over your receipts, and
hand you the original files to download. Upload a pile of PDFs/images, the
worker OCRs and classifies them, renames and files them by category, and the
chat tab can then answer questions like "how much did I spend on vet bills
last year?" with citations back to the original files.

Everything runs locally, single-user, no auth — this isn't meant to be
exposed on the public internet (see [Notes / next steps](#notes--next-steps)).
The LLMs run in **Ollama on your host** (so they use your GPU); the rest runs
in Docker, fronted by an `nginx` HTTPS reverse proxy — see [Run](#run) below.

```
┌────────────┐   HTTP    ┌─────────────┐   SQL     ┌──────────────────┐
│  frontend  │ ────────▶ │   backend   │ ────────▶ │ postgres+pgvector │
│  (Next.js) │ ◀──SSE─── │  (FastAPI)  │           └──────────────────┘
└────────────┘           │             │   jobs    ┌──────────────────┐
                         │             │ ────────▶ │      redis        │
                         └─────┬───────┘           └────────┬─────────┘
                               │                            │
                               │  /api/chat, /api/embed     │ arq
                               ▼                            ▼
                      ┌──────────────────┐          ┌──────────────────┐
                      │ Ollama (on host) │          │  worker (arq)    │
                      │ qwen2.5:14b      │          │  OCR→extract→    │
                      │ qwen2.5vl:7b     │◀─────────│  classify→embed  │
                      │ bge-large        │          └──────────────────┘
                      └──────────────────┘
```

`nginx` fronts `frontend` + `backend` with HTTPS and is the app's only
published entrypoint (`frontend` doesn't publish a host port at all). Every
service has `restart: unless-stopped`, so once it's up it survives reboots
and Docker restarts on its own — this is meant to be left running like a
small personal server, not started by hand each time. One more piece sits
on top of this core, off by default:

- **speech-service** (`speech-service/`) — host-side speech-to-text for the
  chat tab's mic button. Not Dockerized (GPU access), not required for the
  app to work. If it isn't running, the mic button just disables itself. Can
  optionally be registered to start automatically at login (Windows) or
  boot (Linux/Mac) too — see [Run](#run) below.

## Prerequisites

1. **Docker Desktop** (Windows/Mac) or Docker Engine + compose plugin (Linux).
2. **Ollama** installed and running on the host with GPU support:
   <https://ollama.com/download>
3. Copy `.env.example` to `.env` and configure it — at minimum look at
   `DOMAIN_NAME` (for the HTTPS cert) and `OLLAMA_BASE_URL` (if Ollama isn't
   on the default host/port). This is required, not optional: `run.ps1`/
   `run.sh` (below) fails fast if `.env` doesn't exist yet, rather than
   silently defaulting it for you.

   ```bash
   cp .env.example .env
   ```

## Run

Everything — dependency checks, model pulling, HTTPS cert, voice input,
build, start — is one script, at the repo root (no `scripts/` directory;
this used to be four separate scripts, now it's all inline in one file per
platform):

```powershell
# PowerShell
./run.ps1
```

```bash
# bash
./run.sh
```

Safe to re-run any time — every step is a no-op if it already happened
(existing models/cert are detected and skipped). What it does, in order:

1. **Docker** — checks it's installed *and actually running* (not just on
   PATH), and that the compose plugin is available.
2. **`.env`** — fails with instructions if it doesn't exist yet (see
   [Prerequisites](#prerequisites)). Everything past this point reads
   settings from it.
3. **Ollama models** — pulls whichever of `OLLAMA_LLM_MODEL`,
   `OLLAMA_VISION_MODEL`, `OLLAMA_EMBED_MODEL`, `OLLAMA_EXTRACT_MODEL` (from
   `.env`, default `qwen2.5:14b` / `qwen2.5vl:7b` / `bge-large` / unset)
   aren't already installed (checked via `ollama list`).
4. **HTTPS cert** — self-signed, for `DOMAIN_NAME` (defaults to
   `localhost`; set `HTTP_PORT`/`HTTPS_PORT` too if 80/443 are already
   taken on your host — common on Windows, IIS/Skype/VMware often grab
   443). Generated inside a throwaway Docker container (`alpine` + openssl,
   removed immediately after) — nothing extra to install on the host.
   Written to `./certs/`, reused on every future run; pass `-Force`/
   `--force` to rotate it (e.g. after changing `DOMAIN_NAME`). It's
   self-signed, not Let's Encrypt — there's no public DNS record for this
   app to satisfy an ACME challenge against, by design (see
   [Notes / next steps](#notes--next-steps)). Your browser will warn until
   you import `./certs/fullchain.pem` yourself or click through the
   warning. If browser uploads should go over HTTPS too (they bypass the
   Next.js proxy straight to the backend — see `NEXT_PUBLIC_BACKEND_URL`
   comments in `.env.example`), set
   `NEXT_PUBLIC_BACKEND_URL=https://<DOMAIN_NAME>/backend` (append
   `:<HTTPS_PORT>` if you changed it from 443) in `.env` before building.
5. **Voice input** — asks:
   ```
   [N] No (default) - the mic button stays disabled
   [Y] Yes, start it now (background, until you reboot/log out)
   [B] Yes, start it now AND register it to start automatically every time you log in
   ```
   `Y`/`B` launch it detached (no visible window), logging to
   `speech-service/speech-service.log` (and `.err.log` on Windows) — tail
   that for startup progress (first run downloads the Whisper model) or to
   diagnose a problem. `B` additionally registers it to start on its own
   going forward: a Task Scheduler entry on Windows (`Register-ScheduledTask`,
   trigger "at log on" — not literally at system boot before login), or a
   `crontab -e` `@reboot` entry on Linux/Mac (genuinely at boot). To remove
   that registration later: `./run.ps1 -RemoveSpeechServiceStartup` /
   `./run.sh --remove-speech-service-startup` (or do it manually — Task
   Scheduler, find `DocstoreSpeechService`, delete it; `crontab -e`, delete
   the line mentioning `run.sh`). If you never plan to use voice input,
   there's nothing to turn off — just answer `N` (or hit enter) and don't be
   surprised the mic button stays disabled.
6. **Build + start** — `docker compose up --build -d`.

Once it's up:

- App: `https://<DOMAIN_NAME>/` (`https://localhost/` with the defaults).
  `http://` on `HTTP_PORT` redirects to HTTPS automatically.
- Backend API docs: `http://localhost:8000/docs` (still published directly).
- Uploaded originals + renamed copies land in `./storage/` (bind-mounted, so
  they survive `docker compose down`).
- Postgres data lives in `./data/postgres/` (bind-mounted, same reason).
- `db` and `redis` publish no host ports — reachable only from other
  containers on the `docstore` compose network, by service name (`db:5432`,
  `redis:6379`). `frontend` publishes nothing either — only reachable
  through `nginx`. `backend` (8000) and `nginx` (`HTTP_PORT`/`HTTPS_PORT`)
  are the only ports exposed to the host. Need a GUI client against
  Postgres? Temporarily add a `ports: ["5432:5432"]` line to `db`, or
  `docker compose exec db psql -U docstore`.

### Running the speech service standalone

`run.ps1 -SpeechServiceOnly` / `run.sh --speech-service-only` runs just the
speech service in the foreground (no dependency checks, no Docker) — this
is what the background launch and the login/boot registration both actually
invoke under the hood; run it directly yourself if you'd rather watch its
output live in its own terminal instead of tailing the log file. First
time, install its dependencies into whatever Python environment you'll run
it with: `pip install -r speech-service/requirements.txt`. Optional: run
`speech-service/smoke_test.py` first to check whether your GPU actually
works with faster-whisper (Blackwell GPUs currently fall back to CPU — see
comments in `speech-service/server.py`); a `small` model transcribing a
short clip on CPU is still just a few seconds.

The backend polls `GET /health` on the speech service every time the
frontend asks `GET /speech/status` (which the chat tab does on load and
every 30s), and the mic button enables/disables itself accordingly — stop
the service and the button disables itself again within ~30s, no restart of
`docker compose` required either way.

### Postgres bind mount on Windows

`db` stores its data in `./data/postgres` (a bind mount, not a named volume) so
it's easy to find/back up. On Docker Desktop's WSL2 backend this normally just
works, but Postgres is picky about data-directory permissions and can
occasionally refuse to start ("data directory has invalid permissions") when
the mount is backed by the Windows NTFS filesystem. If that happens, either:
- move the whole project under your WSL2 home (`\\wsl$\...` / `/home/...`)
  instead of `C:\Users\...`, or
- switch `db`'s volume back to a named volume (`pgdata:/var/lib/postgresql/data`
  + a top-level `volumes: { pgdata: }`) — you lose the "just a folder" browsing
  convenience but sidestep the permission translation entirely.

### Ollama connectivity

Containers reach the host via `host.docker.internal`. On Linux this is wired up
with `extra_hosts: host-gateway` in the compose file. If Ollama listens on a
non-default port or address, set `OLLAMA_BASE_URL` in `.env`, e.g.
`OLLAMA_BASE_URL=http://host.docker.internal:11434`.

If you run Ollama itself in Docker, point `OLLAMA_BASE_URL` at that service
instead and give it the GPU (`--gpus all` / compose `deploy.resources`).

## How ingestion works

Each uploaded file goes through the `worker` (arq) pipeline:

1. **OCR** — PDFs are rasterized per page (PyMuPDF) and each page is transcribed
   by `qwen2.5vl:7b`; images go straight in. Output is layout-preserving
   markdown stored on the document.
2. **Classify + extract** — `qwen2.5:14b` returns structured JSON: category,
   doc type, issuer, dates, currency, total, line items, and free-form "facts"
   (e.g. `SIN`, `employer`, `account number`), plus a suggested filename.
3. **Rename + file** — the original is copied to
   `storage/<category>/<canonical-name>` and the path is saved on the row.
4. **Chunk + embed** — OCR text is chunked and embedded with `bge-large`;
   vectors go into `chunks.embedding` (pgvector, HNSW + cosine).

Watch progress in the UI (per-file status) or `GET /jobs`.

## How chat works

`POST /chat` runs a tool-calling loop against `qwen2.5:14b`. Tools:

| tool | purpose |
|---|---|
| `search_documents` | vector search over chunks, with category/type/date filters |
| `query_line_items` | SUM / list line items — used for "how much did I spend on…" |
| `lookup_fact` | find a value across documents' extracted facts (SIN, employer, …) |
| `list_documents` | enumerate documents by category / type / date |
| `get_document` | full metadata + summary + download URL for one document |

The final answer is streamed to the browser; cited documents come back as a
`sources` event and render as download links.

Voice input (mic button) posts a clip to `POST /speech/transcribe`, which the
backend forwards to the optional host-side speech service; the transcribed
text is appended to (not sent instead of) whatever's already typed. `GET
/speech/status` is what the frontend polls to enable/disable the button.

## Project layout

```
run.ps1             # one-command setup + launch (PowerShell) - see Run
run.sh              # same, bash
docker-compose.yml
.env.example
db/init/            # creates the vector extension on first boot
backend/
  app/
    main.py         # FastAPI app + startup schema/index creation
    config.py
    db.py
    models.py       # documents, line_items, chunks, jobs
    ollama_client.py
    speech_client.py  # client for the optional host-side speech service
    storage.py
    routers/        # documents, jobs, chat, speech
    ingest/         # ocr, classify, extract, chunk, pipeline
    agent/          # loop.py (tool loop), tools.py (tool impls)
    worker.py       # arq WorkerSettings
frontend/
  app/              # Next.js App Router: chat + upload UI
  app/api/          # thin proxy routes to the backend
speech-service/     # optional host-side speech-to-text (faster-whisper)
nginx/templates/    # HTTPS reverse-proxy config (envsubst template)
certs/              # self-signed TLS cert/key, generated locally, gitignored
```

## Notes / next steps

- Schema is created on backend startup (`Base.metadata.create_all` + index DDL).
  For real migrations add Alembic later.
- Single-user; there's no auth. Don't expose the ports publicly — `nginx`
  encrypts LAN traffic and lets you use a real domain name, but it doesn't
  add authentication or make public exposure safe.
- `EMBED_DIM` must match the embedding model. `bge-large` = 1024. If you swap
  models, change it and recreate the `chunks` table.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
