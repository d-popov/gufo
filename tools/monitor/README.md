# gufo-monitor

A web page that shows what a running `gufo serve llm` is doing:

- the server state (Loading, Waiting, Reading prompt, Generating with its rate, Down);
- live decode and prefill rates;
- requests running and queued;
- cache reuse and draft acceptance;
- GPU busy, GTT memory, temperature and power, plus host memory and CPU;
- one row per session, grouped by served model when sessions report one;
- recent requests from the log;
- token and request totals;
- averages over the last 24 hours and 7 days ([Last 24 h / 7 days](#last-24-h--7-days));
- optionally, the server's named API keys with their usage, which an admin can
  create, edit and revoke ([API keys](#api-keys));
- optionally, a public portal where friends buy prepaid credits with USDC and create
  their own metered API keys ([Credits portal](#credits-portal)).

One Python process (standard library only, Python 3.10 or later) polls Gufo, sysfs and
`/proc` once per second and serves the pages on one listener: the monitor at `/monitor`
and, with `--billing-config`, the public credits portal at `/`. Any number of browsers
share those polls: `GET /api/state` returns a snapshot with history, and
`GET /api/stream` sends one Server-Sent Event per poll. The pages have no build step and
load nothing from other hosts.

The pages include a chat at `/chat` (**Chat** in the top menu) for the admin and, with
`--billing-config`, for approved portal users with a positive balance. It sends text and
image attachments through the monitor, so no API key reaches the browser. The admin chats
with the key in `--chat-key-file` (an inference key, such as the owner's), or else the
monitor's own `--gufo-key-file` key, which then must allow inference: a read-only
`access=monitor` key makes Gufo refuse chat, and the chat says so. A user chats with a server-held **Web chat** key, derived from
the admin token and listed with their keys, so those requests are metered and billed like
any other. Replies stream as they are generated, with the model's thinking when it has
any, and each reply shows its speed (tok/s), time to first token, token counts and, with
`--billing-config`, its price at the model's rates (the admin sees list prices; only
users are charged). The chat's running total (cost and tokens) stays in view above the
conversation. **Thinking** sets the model's reasoning effort (Default leaves it to the
server; Off, Low, Medium, High and Max are sent as `reasoning_effort`). Images attach with
the button or by pasting (Ctrl+V). The browser keeps every conversation in its own database
(IndexedDB), with no limit but the browser's quota and nothing stored on the monitor; the
chat list shows the 200 most recent of the open project. A reply keeps being written when the page goes to the background or loses its network (a phone
switching apps): the monitor generates it in a run that does not depend on the browser's connection,
and the page reads the rest when it is back, from the event where it stopped. **Stop** ends the run
and the generation; a run nobody has read for three minutes is stopped too, and a finished one stays
readable for ten (kept in the monitor's memory only, for the user who started it). **Delete** removes the open chat,
after a second click to confirm. Image bytes are not kept. Moving to another browser or
device does not bring the chats along: use a project's Export for its notes.

**Projects.** The first box in the toolbar picks a project: a folder of chats that share two
plain Markdown notes, both editable in the **Project** panel.

- **Guidelines**: rules for the model in every chat of the project, like a `CLAUDE.md`.
  You write them; **Draft guidelines** writes a first version from a sentence about the
  project and refines what is there. Edits apply to the next message, even in an open chat.
- **Memory**: what the model remembers from the project's chats. It updates itself when you
  leave a chat with new answers (or open another project), after a minute without activity
  once a reply is done, and on **Update memory from this chat**; the model is asked to merge
  the new conversation into the notes (at most 400 words), at the cost of one short request
  each time, billed like any other. Untick *Update when I leave a chat* to keep it manual.
  A chat keeps the memory it started with, so an update never changes the start of its
  prompt and forces it to be read again; new chats get the updated one. If you edit the
  memory while an update runs, your edit is kept.

Both go to the model as one system message ahead of the conversation. **Export .md** downloads
the two notes as one file with `# Guidelines` and `# Memory` headings; **Import .md** reads
such a file back (a file with one heading replaces only that note). Deleting a project keeps
its chats, which move to *No project*.

The **Tools** menu above the message box chooses what the model may use (nothing by default;
the choice is remembered). The chat sends the model only the ticked tools, with a short system
note, runs its tool calls itself, and sends what they found back until it answers (at most
six model calls per message, each billed like any other). A tool the model asks for that was
not ticked is refused. **Stop** cancels the reply and whatever its tools were doing.

**Web search** and **Read pages.** Every web request is made by your browser; the inference
server and the monitor fetch nothing. A browser can only read a response when the site allows
it (CORS), so `fetch_url` asks the site directly, without cookies, and works for Wikipedia,
GitHub, Hacker News and other public APIs. A site that does not allow it, and every search,
is read through the public reader [r.jina.ai](https://r.jina.ai) (DuckDuckGo Lite for
search), which then sees that URL or query; those steps are labelled "via r.jina.ai" in the
thread, and it limits anonymous use to about 20 requests a minute. Set `READER` to `''` in
`static/webtools.js` to use only sites that allow direct reads (and no search). Local,
private and IP-address URLs are never fetched, and page text is marked as untrusted in the
system note. Only the chat page may connect to https sites (`connect-src 'self' https:`);
every other page keeps `connect-src 'self'`.

**Pictures and video.** With **Pictures and video** ticked, the model can ask `find_media` for
freely licensed photos or short clips on [Wikimedia Commons](https://commons.wikimedia.org),
asked from your browser like the other web tools. They appear in the thread as a grid of
thumbnails, each linked to its Commons page with its license and author; videos play in place, from Commons' VP9 WebM rendition nearest 480p (about 1 MB per second, instead of an original that can be 100 MB). Commons has no H.264 rendition, so a browser without WebM/VP9 support (iOS before 17.4) cannot play them inline: the caption link opens the file on Commons.
The model names what to look for and gets titles, credits and page links back; it never supplies
an address, and the page draws only files on `upload.wikimedia.org` and `thumb.wikimedia.org`,
which are the only image and media hosts the chat page's CSP allows. Nothing is hosted or
copied by the monitor. Pictures are loaded straight from Wikimedia by your browser and are
re-fetched when a saved chat is reopened. Commons holds photos and clips, not every
web image; search results from the open web are not shown as pictures.

**Python** runs the model's code in your browser, in a sandbox, and shows the charts it
draws (matplotlib) under the step, each with a **Save PNG** link. numpy, pandas and
matplotlib are there; variables stay between calls until the box is reset by a timeout (30 s)
or **Stop**. It needs the Pyodide runtime (CPython in WebAssembly), which is not in the
repository: run `python3 tools/monitor/fetch-pyodide.py` once (about 30 MB, pinned and
checked against SHA-256 sums; it adds no host to the page's policy) and restart the monitor.
Until then the menu shows Python as not installed. The first use downloads the runtime into
the browser, which then keeps it. The box is a module worker, `static/pyworker.js`, served
with a policy of its own, `default-src 'none'; connect-src 'none'`, so the model's code has
no network (not `fetch`, `XMLHttpRequest`, `WebSocket` or the prototype's `fetch`), no
cookies or storage of the monitor, and no files of the host. The page hands it the runtime's
files, which only signed-in chat users can download. Figures are not kept after a reload.

## Run

```sh
python3 tools/monitor/gufo-monitor.py --gufo http://127.0.0.1:8080
```

Open `http://127.0.0.1:8099/?admin`, or choose **Admin sign-in** in the profile menu.
Enter the admin token in the portal's **Admin** authentication tab; the shared header
then links to the monitor (`/monitor`). If the token file does not exist, the monitor creates it on first start at
`$XDG_CONFIG_HOME/gufo-monitor/admin-token` with mode 0600. The monitor is the admin's
alone: nobody else sees it, from any host, and the admin sign-in lasts 12 hours. When it
ends, the open monitor page says so and links to the sign-in.

Scripts send the admin token as `X-Admin-Token: <token>` or
`Authorization: Bearer <token>`.

| Option | Default | Meaning |
|---|---|---|
| `--gufo URL` | `http://127.0.0.1:8080` | Gufo base URL |
| `--gufo-key-file FILE` | none | file holding the server's `--api-key`; read and used only by the monitor |
| `--chat-key-file FILE` | the monitor's key | file holding an inference key for the admin's chat; keeps `--gufo-key-file` read-only |
| `--chat-model NAME` | the first served model | the model the chat page selects until a user picks another; the page then remembers that choice in the browser |
| `--bind`, `--port` | `127.0.0.1`, `8099` | listen address of the monitor and the portal |
| `--log-source` | `none` | `podman:CONTAINER`, `file:PATH` or `none` |
| `--log-since` | `1h` | podman log backlog read at start |
| `--log-utc` | | `file:` log timestamps are UTC (Gufo inside a container) |
| `--poll`, `--history` | `1`, `900` | poll interval and sparkline window, seconds |
| `--gpu-card` | `auto` | DRM card, for example `card1` |
| `--show-clients` | | show client ids from the log |
| `--secure-cookie` | | always mark the sign-in cookies `Secure` |
| `--keys-file FILE` | none | the server's `--api-key-file`; enables the API keys section |
| `--admin-token-file FILE` | `$XDG_CONFIG_HOME/gufo-monitor/admin-token` | admin token, the only way into the monitor: browser sign-in, scripts, keys and accounts; created if missing |
| `--billing-config FILE` | none | enable the credits portal ([Credits portal](#credits-portal)); needs `--keys-file` |
| `--env-file FILE` | `.env` next to the script if present, else `$XDG_CONFIG_HOME/gufo-monitor/.env` | settings such as `GUFO_PUBLIC_URL` ([Public URL](#public-url-env)); a missing default file is fine |
| `--stats-file FILE` | `$XDG_CONFIG_HOME/gufo-monitor/stats.sqlite` | hourly sums for the last 24 h / 7 days tile; created (mode 0600) if missing |

Without `--log-source`, the recent-requests table and the request counts stay empty.
Everything else works.

A systemd user unit, as `~/.config/systemd/user/gufo-monitor.service`:

```ini
[Unit]
Description=gufo-monitor

[Service]
ExecStart=/usr/bin/python3 /path/to/gufo/tools/monitor/gufo-monitor.py --gufo http://127.0.0.1:8080 --gufo-key-file %h/.config/gufo/api_key --log-source podman:gufo
Restart=on-failure

[Install]
WantedBy=default.target
```

## Where the numbers come from

- **Decode and prefill tok/s:** from `/slots` polls (`next_token[0].n_decoded` and
  `n_prompt_tokens_processed`) for each session, summed over sessions. A session's
  decode rate covers its current request from the first generated token, up to the last
  30 s. It appears once that span reaches 3 s, because shorter spans are skewed by the
  partial first poll interval and by speculative bursts. Until then the pill shows
  "Generating" without a rate, and the session is left out of the total. The token
  counters in `/metrics` only grow when a request completes, so they cannot give a live
  rate.
- **Cache reuse and draft acceptance:** `/metrics` counter differences over the last
  5 minutes, so they cover completed requests only. A counter that drops means Gufo
  restarted, and the next poll starts a new baseline.
- **Recent requests:** the `event=completed` lines of Gufo's log. Only the fields the
  table shows are kept. Prompts and outputs are never read, and client ids appear only
  with `--show-clients`.
- **Images:** requests with images carry `images`, `image_tokens`, `image_bytes`,
  `image_px`, `image_px_hits`, `image_px_misses`, `encoder_images`, `encode_ms` and
  `image_tokens_reused` on their completed line. The recent-requests table then shows an
  **Images** column (`2 img · 3.1k tok · 410 ms enc`, the rest in its tooltip), empty for
  text requests. The pixel cache holds decoded and resized pixels only: a hit skips
  decoding, not the vision encoder, which runs unless continuation reuses the image's
  tokens. The 24 h / 7 d tile shows the average encoder time per image request. A field
  Gufo does not log (older builds log none) shows as nothing, never as 0.
- **Telemetry:** from `/sys/class/drm/cardN/device` (`gpu_busy_percent` and
  `mem_info_gtt_*`), its `amdgpu` hwmon (temperature, power, clock), `/proc/meminfo` and
  `/proc/stat`.

### Last 24 h / 7 days

The last tile shows the headline figures over two windows: decode tok/s (the big
number), prefill tok/s, tokens/h, image encoder ms per image request and, with
`--billing-config`, revenue/h and revenue/day (users plus our own requests). The portal's **Usage** tab (admin) lists every other figure below. **24 h** is the current UTC hour and the 23
before it, **7 d** the current hour and the 167 before it (so a window spans between
N − 1 and N hours). The data are hourly sums in `--stats-file`, a SQLite file (WAL,
mode 0600) separate from billing, kept 8 days. The page reads them from
`GET /api/stats/long` (admin only, like `/api/state`) once a minute.

- **Requests:** completed inference requests from the log, `POST /v1/...` (the probe
  and monitoring routes of the recent-requests table never count). Each counts once,
  by its log id and time (`request@t`), however often the log is replayed: by a
  restarted tail, a restarted monitor or the backfill.
- **Decode tok/s** (the big number, 24 h, with 7 d beside it): generated tokens over the
  time spent decoding them, Σ `generated_tokens` / Σ (`generated_tokens` /
  `decode_tps`), over requests that report a `decode_tps`. A long answer weighs more
  than a short one, unlike a mean of per-request rates.
- **Prefill tok/s:** the same with `prefill_tokens` and `prefill_tps`.
- **Tokens/h:** generated tokens over the hours the window has log data for. A
  utilization figure, not a rate: idle hours lower it.
- **Serving time %:** the share of the 1 s polls with at least one request running or
  queued. Summed request durations would overcount concurrent requests.
- **GPU busy %, GPU power W:** means of the polled `gpu_busy_percent` and power.
- **Revenue/h, Revenue/day:** what portal users paid for usage (`usage` ledger debits)
  plus our own requests (keys outside the portal, valued at the prices of the moment and
  never charged), in the rolling last 24 h or 168 h, divided by the window's hours (×24
  for a day), so an idle or young window averages low. The tile shows the sum; the
  Usage tab also lists users and internal separately. Without `--billing-config` the
  revenue is `null` and the rows show `—`.
- **Errors:** requests with status 400 or above. **Cache reuse %:** Σ `cached_tokens` /
  Σ `prompt_tokens` (a prompt's tokens include its cached ones).
- **Image enc ms/req:** Σ `encode_ms` over the requests with images that report it,
  divided by their number; `—` without any. `/api/stats/long` also has the sums
  `image_requests`, `images`, `image_tokens`, `encoder_images`, `encode_ms` and
  `image_tokens_reused`. Stores from before these columns gain them on start (0 in
  older hours).

Coverage, in the Usage tab under the table: **log since** the first log line the store has seen, when
that is later than the 7-day window's start, and **GPU *a* / *b* h sampled** when the
polls cover less than the windows. Request figures are averaged over the covered hours
only (tokens/h), or are sums of what the log holds; GPU figures and serving time come
from the live monitor only and cannot be backfilled, so they start when the monitor
(with this store) first ran and have gaps while it was stopped.

**Backfill:** with `--log-source file:PATH`, when the store's log data start later
than 7 days ago (a new store, or a young log), the monitor reads `PATH.1` (if present)
and `PATH` from their first lines once at start, in a background thread, through the
same count-once path; only the fields of the recent-requests table are read. Requests
older than 8 days are skipped. A `podman:` source has no backfill: its data start with
the `--log-since` backlog.

Pending sums are written once a minute (and on Ctrl-C), so stopping or a crash loses
at most a minute of GPU samples; its requests count again when the restarted monitor
re-reads the log.

## API keys

With `--keys-file`, the page lists the named keys of Gufo's `--api-key-file`. For each
key it shows its access, its models and its usage from the log: requests, prompt,
cached and generated tokens, errors and last use. It also reports whether Gufo
applied the latest version of the file.

After signing in as admin (**Admin sign-in** in the profile menu, on either page), you
can, on either page:

- create a key: inference or monitoring only, all models or chosen models. The page
  shows the new token once; the file stores only its SHA-256 digest;
- change a key's access or models;
- revoke a key. Its clients are refused within about a second, when Gufo re-reads
  the file. The last key cannot be removed, because Gufo refuses a file without keys.

Edits keep comments and other lines, and replace the file atomically. If Gufo runs in
a container, mount the file's directory, not the file itself, so Gufo sees the
replaced file:

```sh
podman run ... -v ~/.config/gufo/keys:/keys:ro ... gufo serve --api-key-file /keys/api_keys llm ...
python3 tools/monitor/gufo-monitor.py --gufo http://127.0.0.1:8080 \
  --gufo-key-file ~/.config/gufo/monitor_key --keys-file ~/.config/gufo/keys/api_keys \
  --log-source podman:gufo
```

The monitor's own Gufo key can be a monitoring key (`access=monitor`): it only needs
`/ready`, `/v1/models`, `/props`, `/slots` and `/metrics`.

## Credits portal

With `--billing-config`, the same listener also serves a public portal at `/`. Friends
sign up there, the admin verifies and approves them, and each approved account gets a
one-time signup credit. Users top up with USDC from a browser wallet, on Solana
(Phantom, Solflare, Brave Wallet) or Base (MetaMask), and create their own Gufo API
keys. Every completed request of their keys in Gufo's log is debited from their
balance. When the balance reaches zero, their keys leave Gufo's `--api-key-file`; a
deposit or an admin credit puts them back.

Public routes are `/`, `/static/*` and `/api/portal/*` (plus the admin sign-in,
`POST /api/admin/login`); they never expose monitor data. `/monitor` and every other
`/api/` route are the admin's alone: a signed-in portal user sees only their own account,
keys and requests, never the monitor. The portal needs no SMTP server: the
verification link is shown to the admin, who sends it on, or verifies the address with
one click after checking who signed up.

Both pages use the neutral **AI Inference** brand with an original geometric owl mark
and share one header: links to the portal and the monitor (the latter only when signed
in as admin) and the profile menu. The menu offers **Sign in**, **Sign up** and
**Admin sign-in** when signed out, and the account's email, status and balance with
**Sign out** when signed in. Authentication uses a centered portal card with
keyboard-accessible **Sign in** and **Sign up** tabs (`/?signin`, `/?signup`).
The **Admin** tab is hidden by default: **Admin sign-in** in either page's profile
menu (or `/?admin`) reveals it and focuses the token field. It stays open while
focus moves within the whole authentication card, including the submit button.
Moving focus outside the card, selecting another auth tab, or pressing Escape
dismisses Admin and clears its token and error; Escape returns focus to the profile
button. A signed-in user's auth card is hidden again on dismissal. Without the
credits portal, the profile menu still opens Admin sign-in. Signed in as admin,
the portal page shows an **Admin**
area with tabs: **Accounts** (sign-ups, verification links with **Verify**, balances,
wallets and keys; approve, reject, suspend, credit), **API keys** (all keys of
Gufo's key file), **Pricing** (the editable price list, see [Pricing](#pricing)),
**Usage** (internal vs user costs, see [Usage](#usage-admin-only)) and **Deposits**
(the receiving wallet audit, see [Deposit audit](#deposit-audit-admin-only)). The
monitor page shows the same panels.

The pages share a responsive light/dark design system and a local subset of Lucide
SVG icons (`static/icons.js`), rendered through DOM APIs without fonts, external
requests or inline scripts. Icons accompany text labels, with `currentColor` strokes.
The geometric brand mark and favicon are original; Lucide's ISC and Feather-derived
MIT notices are retained in `licenses/lucide.txt` and `THIRD_PARTY_NOTICES.md`.

### Setup

1. Run Gufo with an `--api-key-file` that holds your own key (the "owner" key) and
   mount its directory, as in [API keys](#api-keys). The monitor refuses to start
   billing if the file has no key besides portal keys, because Gufo refuses a file
   without keys and would keep serving the last ones.
2. Copy `billing.example.json` and `pricing.example.json` somewhere private and edit
   them ([Configuration](#configuration)).
3. Start the monitor with `--log-source` (metering reads Gufo's log), `--keys-file` and
   `--billing-config`:

   ```sh
   python3 tools/monitor/gufo-monitor.py --gufo http://127.0.0.1:8080 \
     --gufo-key-file ~/.config/gufo/monitor_key --keys-file ~/.config/gufo/keys/api_keys \
     --log-source podman:gufo --billing-config ~/.config/gufo-monitor/billing.json
   ```

4. Put your public URL in `tools/monitor/.env` ([Public URL](#public-url-env)) and
   publish the monitor's port, for example through a
   [Cloudflare tunnel](#cloudflare-tunnel). Gufo's own API needs its own route at
   `GUFO_API_URL`.
5. On the portal page, sign in as admin from the profile menu. **Accounts** lists
   sign-ups, their verification links, balances, wallets and keys; it verifies,
   approves, rejects, suspends or credits accounts.

### Public URL (.env)

Verification links, wallet-link messages and the API URL shown to users are built from
settings, never from a request's `Host` header. Copy `tools/monitor/env.example` to
`tools/monitor/.env` (git-ignored) or `$XDG_CONFIG_HOME/gufo-monitor/.env`, or pass
`--env-file FILE`. The file holds `KEY=VALUE` lines with `#` comments and optional
quotes; nothing is expanded. Variables in the process environment win over the file.

| Variable | Default | Meaning |
|---|---|---|
| `GUFO_PUBLIC_URL` | `http://<bind>:<port>` | the public URL of the monitor's listener, e.g. the tunnel's `https://gufo.example.com`; `http` or `https`, host only, trailing slash dropped |
| `GUFO_API_URL` | `GUFO_PUBLIC_URL` + `/v1` | Gufo's public OpenAI-compatible URL, shown to users. The monitor does not proxy `/v1`: route it to Gufo in the tunnel or use Gufo's own hostname |

The billing config still accepts `public_base_url` and `api_base_url` as fallbacks
when these variables are unset.

### Cloudflare tunnel

A tunnel publishes the monitor without opening a port. For a quick test:

```sh
cloudflared tunnel --url http://127.0.0.1:8099
```

prints a random `https://….trycloudflare.com` URL; put it in `GUFO_PUBLIC_URL` and
restart the monitor. For a fixed hostname, create a named tunnel and route it, with an
ingress rule for Gufo's API if users should reach it through the same hostname:

```yaml
# ~/.cloudflared/config.yml
tunnel: gufo
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: gufo.example.com
    path: ^/v1/
    service: http://127.0.0.1:8080
  - hostname: gufo.example.com
    service: http://127.0.0.1:8099
  - service: http_status:404
```

Keep `--bind 127.0.0.1`: `cloudflared` connects from loopback. Requests through it carry
`CF-Connecting-IP` and `X-Forwarded-*`, so sign-in rate limits count the visitor's
`CF-Connecting-IP`, and cookies are `Secure` because Cloudflare sends
`X-Forwarded-Proto: https`. A loopback connection gets no special trust: the tunnel
makes every request look local, so the monitor admits the admin only, whoever connects.

### Configuration

`--billing-config` is a JSON file; relative paths in it are relative to the file.

| Field | Default | Meaning |
|---|---|---|
| `db` | required | SQLite database (created; keep it private and backed up) |
| `pricing` | required | prices file, below |
| `signup_grant_micro` | `990000` | credit granted once on approval, micro-USD |
| `max_keys_per_user` | `5` | active keys per account |
| `model` | served model | default model displayed in the portal and its example prices; does not restrict key access |
| `solana` | absent: disabled | `rpc`, `receiver_owner` (your wallet), `receiver_token_account` (its USDC token account; optional, default: the wallet's associated USDC account), `mint` (USDC), `commitment` (`finalized`) |
| `base` | absent: disabled | `rpc`, `receiver` (your address), `usdc` (USDC contract), `confirmations` (`12`) |

Deposits are checked against your receiving account only: a Solana deposit counts when
the USDC balance of `receiver_token_account` grows in a successful transaction at the
configured commitment; a Base deposit counts when the USDC contract logs a `Transfer` to
`receiver` and the block has `confirmations` blocks on top of it. Public RPC endpoints
are rate limited; a private RPC URL works better for many users.

Without `receiver_token_account`, deposits go to the associated token account of
`receiver_owner` for `mint` (the address wallets use, as `spl-token address --owner`
shows). It may not exist yet, e.g. for a new wallet that never held USDC: the
transaction the portal prepares for a top-up then first creates it with the Associated
Token Account program's `CreateIdempotent`, the payer paying its one-time rent (about
0.00204 SOL; the portal says so before the wallet asks for approval), and the deposit is
accepted with "before" recorded as 0. An explicit `receiver_token_account` is used as it
is; if it does not exist, top-ups fail with `receiver_account_missing`, as only the
associated account can be created on the fly.

### Pricing

Money is integer micro-USD everywhere (1 USD = 1,000,000; USDC has 6 decimals, so one
USDC base unit is one micro-USD). Prices are micro-USD per 1M tokens:

```json
{"default": {"input": 225000, "cached_input": 22500, "output": 705000},
 "models": {"qwen3.8-flash-next": {"input": 225000, "cached_input": 22500, "output": 705000}}}
```

The defaults are $0.225 per 1M input tokens, $0.0225 per 1M cached input tokens and $0.705
per 1M output tokens. A request costs

```
ceil(((prompt_tokens - cached_tokens) * input + cached_tokens * cached_input
      + generated_tokens * output) / 1000000)
```

micro-USD, from its `event=completed` log line. Gufo's `prompt_tokens` counts the whole
prompt, so the cached part is billed only at the cached price. Requests with a model
listed under `models` use its prices; others use `default`. The portal shows users the
prices for its default displayed model (`model` in the config, else the primary served
model): its own entry when it has one, else `default`. Actual charges always use the
model in each completed request, not the portal's display default.

Portal keys default to **all inference models**, including both served session classes
and models added later; they never grant monitoring access. The billing database owns
their model restrictions and revocation state. Admin **Keys** edits (the existing
`PUT /api/keys/NAME` and `DELETE /api/keys/NAME` endpoints) persist across the periodic
sync, credit cutoff/restoration, suspension and monitor restart. Attempting to change
a portal key to `access=monitor` is rejected. Ordinary owner and monitoring keys remain
file-owned. Direct edits to a portal key's file line are overwritten by database sync.

On the first start with this version, the database adds a `keys.models` JSON column
in place. All older portal keys migrate to all-model access, intentionally replacing
the previous implicit primary-only restriction (and any temporary file-only edits).
Existing tokens stay valid: no key regeneration is needed. Back up the billing SQLite
database and key file, then restart **only the monitor** with the same billing config,
database and key file. Its initial reconciliation repairs existing funded, approved
users' keys automatically; Gufo reloads the file without an inference-server restart.
Apply any desired explicit restrictions afterwards through Admin **Keys**.


#### Editing prices

Signed in as admin, the **Pricing** tab (also on the monitor page and in the profile
menu) edits the table: one row for **Default** and one per model, in US dollars per 1M
tokens (up to 6 decimals, converted exactly to micro-USD), next to the cost of an
example request of 1000 prompt tokens, 200 of them cached, and 100 output tokens.
**Add model** offers the models Gufo reports; Default cannot be removed. **Save** checks
the table and replaces the prices file atomically (a temporary file in the same
directory, synced, then renamed over it, keeping the file's mode). The new prices apply
to requests billed from then on, without a restart; charges already in the ledger never
change. **Revert** drops unsaved edits.

Scripts use the same API with the admin token: `GET /api/admin/pricing` returns
`{"default": {...}, "models": {...}, "served_models": [...]}`; `PUT /api/admin/pricing`
with `{"default": {...}, "models": {...}}` replaces the whole table and returns it.
Prices are integers from 0 to 10^12 micro-USD per 1M tokens with exactly the fields
`input`, `cached_input` and `output`; `default` is required; at most 64 models, named
with 1 to 128 characters, without surrounding spaces or control characters. An invalid
table gets `400` with a message and changes nothing.

The file follows the same rules, except that it may omit `default`. The monitor reads
it at start only: after editing it by hand, restart the monitor. A save from the admin
page before that restart overwrites the hand edits with the table the page shows.

### Metering and key sync

- Portal keys are named `u<account>-<n>` in Gufo's key file; only those are billed and
  only those are ever added or removed. Other keys, such as yours and the monitor's,
  are left alone. The file stores SHA-256 digests only; a new key's token is shown to
  its owner once.
- Each completed request becomes one `usage` ledger row, unique on its request id and
  log time, and lines older than the newest billed line are skipped, so re-reading the
  log after a restart never bills twice. A request already running when the balance
  runs out still completes, so a balance can end slightly negative.
- The key file holds a portal key exactly when its account is approved and its balance
  is positive. It is updated after every change that can flip this (a debit that empties
  the balance, a deposit, a credit, approval, suspension, key creation or deletion) and
  checked every minute. Gufo applies the file within about a second.
- Pending deposits are re-checked every 30 seconds for up to a day, then rejected.

### Usage (admin only)

The **Usage** tab on the portal, the monitor's Usage section, and **Admin: usage** in
the profile menu compare internal usage with portal users' usage. Choose 7, 30 or 90
UTC calendar days, including today. Cards show each group's cost, the internal share
of combined cost, and prompt-plus-output tokens (cached tokens are part of the prompt,
not added twice). Tables show internal usage by key and both groups' costs by UTC day.

**Internal usage is not charged.** Requests with a named non-portal key, such as
`owner` or a hand-made key, are valued at the current prices when metered. That cost
is saved permanently: later price changes do not reprice old requests. These requests
never use credits, alter balances, write ledger rows or remove keys. Requests without
a key name are skipped. Gufo's monitoring polls are already excluded from request logs.

SQLite gains one `usage_log` table automatically (`CREATE TABLE IF NOT EXISTS`),
shared by internal requests (`kind=internal`, no user) and portal requests (`kind=user`,
with a user id). Each row stores the timestamp, key name, unique
`request@timestamp` reference, model, prompt/cached/output counts and cost in micro-USD.
For users, the row is written in the same transaction as the ledger debit; free requests
are also recorded. Replayed references cannot add another row. No existing columns
or ledger rows change. Users' reported money always comes from usage ledger debits,
not from recomputing prices. Earlier debits without a usage row still count toward
cost and requests, but their token counts are unknown: the UI explicitly notes them
and token totals include only known counts. No tokens are inferred from ledger text.

`GET /api/admin/usage?days=N` requires admin authentication (a portal session is not
enough). `days` defaults to 30 and must be an integer from 1 through 365; invalid
values return 400 with a message. The response has `internal` (`totals`, `by_key`,
`by_day`) and `users` (`totals`, `by_day`). Aggregate fields are `requests`, `prompt`,
`cached`, `generated`, `cost_micro`; user aggregates additionally carry
`tokens_unknown_requests`. Keys use `key`, days use `day` (`YYYY-MM-DD`, UTC).

### Deposit audit (admin only)

Each credited deposit records the receiving wallet's USDC balance right before and
right after it, so the books can be checked against the chain later:

- **Solana:** from the deposit transaction itself, the `preTokenBalances` and
  `postTokenBalances` entries of `receiver_token_account` (USDC mint only) at the
  configured commitment, plus its `slot` and `blockTime`. Exact for that transaction.
  When the transaction created the account (no pre entry, a USDC post entry with the
  receiver as owner), "before" is 0 and the note says `account created by this deposit`.
- **Base:** USDC `balanceOf(receiver)` at blocks N-1 and N, N being the receipt's block
  (stored as `slot`), plus the block's timestamp. Block granularity: other transfers to
  the receiver in block N show in the delta. Many public nodes serve no historic state;
  their errors leave the values empty (never estimated).
- **At credit:** the receiver's balance read live when crediting
  (`getTokenAccountBalance` / `balanceOf` at `latest`) and its time; for reference only,
  as later transfers move it. A failed read leaves it empty and never delays a credit.
- **Delta check:** when both balances are known and after minus before differs from the
  credited amount (several transfers in one transaction or block, fees), the deposit is
  still credited by the usual rules and gets an `audit_note` such as
  `balance delta 1.00 != credited 0.99`.

The `deposits` table gains the columns `receiver_before_micro`, `receiver_after_micro`,
`slot`, `block_time`, `receiver_live_micro`, `live_at` and `audit_note` in place at
startup (`ALTER TABLE ... ADD COLUMN`, once); deposits credited before have them empty.
They are written in the credit's transaction and never change afterwards.

**Receiver identity.** Each credit also stores the receiver it was verified against,
taken from the checker that verified it, never from later config: `receiver_owner`
(Solana wallet / Base address), `receiver_account` (Solana token account / Base address)
and `asset` (USDC mint / contract). Re-verification checks each deposit against its own
stored receiver, using only today's RPC settings (URL, commitment, confirmations). So when
you switch receiver wallets, old deposits stay verifiable against the old receiver and new
deposits use the new one. Deposits credited before identities were stored have them
empty (`identity: "config-fallback"`): they are re-verified against the receiver
configured now, which is wrong for them after a switch; the pane marks them `config`.
These three columns are added in place at startup the same way as the audit columns.

The **Deposits** tab on the portal, the monitor's Deposits section and **Admin:
deposits** in the profile menu list them, newest first, with a **Download CSV** link and
a **Re-verify against chain** button. Routes (admin authentication, like the others):

- `GET /api/admin/deposits?limit=N` (1 to 1000, default 200): `deposits` rows with `id`,
  `t` (credit time), `user_id`, `email`, `chain`, `tx`, `sender`, `amount_micro`,
  `receiver_before_micro`, `receiver_after_micro`, `receiver_live_micro`, `live_at`,
  `slot`, `block_time`, `audit_note`, `receiver_owner`, `receiver_account`, `asset`,
  `identity` (`stored` or `config-fallback`) and `explorer` (Solscan or Basescan URL), and
  `totals` (`credited_micro`, `deposits`) over all credited deposits.
- `GET /api/admin/deposits.csv`: every credited deposit as CSV (UTC ISO times). Text
  starting with `=`, `+`, `-`, `@`, tab or carriage return gets a leading `'` so a
  spreadsheet does not run it as a formula.
- `GET /api/admin/deposits/verify?limit=N` (1 to 50, default 50): fetches the newest
  credited deposits from the chain again, one at a time, 0.25 s apart, and compares
  sender, amount, balances, slot and block time with what is stored. Each result is
  `ok`, `mismatch` (with `problems`), `unavailable` (RPC failure, chain disabled) or
  `skipped` (the run stops checking after 3 RPC failures in a row); `notes` list values
  only one side has, such as balances a Base node no longer serves. Each result carries
  its `identity` status. One run at a time
  (409 `busy` otherwise). It never changes credits or stored values.

### Operations

- **Verify** marks the email verified exactly like the link (status
  `pending_verification` becomes `pending_approval`); the link stops working. It fails
  with `already_verified` for a verified address.
- **Approve** credits `signup_grant_micro` once per account, however often it is
  pressed. Only email-verified accounts can be approved.
- **Reject** and **suspend** take the account's keys out of Gufo's file; **unsuspend**
  restores them if the balance is positive.
- **Credit** adds or removes (negative amount) micro-USD with a note: refunds,
  corrections, gifts.
- **Ledger:** every balance change is a row in the append-only `ledger` table (triggers
  refuse updates and deletes), written in the same transaction as the balance. To audit
  an account: `sqlite3 billing.sqlite "SELECT * FROM ledger WHERE user_id=1"`.
- **Backups:** the database uses WAL; back it up with
  `sqlite3 billing.sqlite ".backup backup.sqlite"` while the monitor runs.
- **Deposits sent from an unlinked wallet** are rejected with `sender_not_linked`. The
  user can link that wallet and submit the transaction again; a credited transaction
  can never be credited again.

### Portal security

- **Admin data:** the account list holds emails and verification links; like the rest
  of the monitor, reading it takes the admin sign-in.
- **Verification tokens** are stored in the database as issued, because the admin page
  must show the link; they are cleared once used. Anyone with the database or the admin
  token can verify an account, which still needs the admin's approval.
- **Passwords:** scrypt (N=2^14, r=8, p=1) with a per-account salt; at least 10
  characters. Login and signup are limited to 10 attempts per minute per client
  address. When the connection comes from loopback (a tunnel or proxy on this host),
  the client is `CF-Connecting-IP`, else the last `X-Forwarded-For` entry; any other
  peer is keyed by its own address, whatever headers it sends. Signing up with a
  registered email answers like a new sign-up.
- **Sessions:** a random 32-byte id in the `gufo_portal` cookie (`HttpOnly`,
  `SameSite=Strict`, 30 days, `Secure` with `--secure-cookie` or behind a proxy sending
  `X-Forwarded-Proto: https`); the database stores only its SHA-256.
- **CSRF:** every POST and DELETE needs the `X-Gufo-Portal: 1` header and a JSON body,
  which a cross-site page cannot send without a CORS preflight the portal never grants.
- **Wallets:** linking signs a single-use message (domain, address, chain, random nonce,
  issue time; valid 10 minutes) with the wallet: Ed25519 for Solana, EIP-191
  `personal_sign` for Base. A wallet belongs to one account, and deposits count only
  when the sender is a wallet linked to the depositing account.
- **Responses** carry the monitor's CSP and headers; the portal loads nothing from
  other hosts, and wallets are reached through the browser extensions only.

## Security

- **Gufo API key:** it stays in the monitor process. It never appears in a response,
  the page or the monitor's own log.
- **Routes:** `/`, `/static/*`, `/api/portal/*`, `POST /api/admin/login`,
  `POST /api/admin/logout` and `GET /api/admin/session` are public. `/monitor` and
  every other `/api/` route (`/api/state`, `/api/stream`, `/api/stats/long`, `/api/keys`,
  `/api/admin/accounts`, ...) are the admin's alone, whoever connects and from where,
  this host included: a portal session is not enough. Without the admin sign-in, API
  routes answer `401` and `/monitor` redirects to `/`.
- **Admin token:** on a public URL it is the key to the kingdom: it reads every
  account's email, approves and credits accounts, creates and revokes Gufo keys and
  opens the monitor. Keep the generated 43-character random token (or another long
  random one) and never share it. Scripts send it as `X-Admin-Token` or
  `Authorization: Bearer`. It is compared in constant time; a wrong token at sign-in
  costs half a second, and sign-in is limited to 10 attempts per minute per client
  address, counted like the portal's (`CF-Connecting-IP` from loopback). Changes are
  JSON requests, so a cross-site form cannot submit them.
- **Admin cookie:** `gufo_monitor_admin` holds its expiry time and an HMAC of it under
  the admin token, never the token; the monitor refuses it after 12 hours, and an open
  event stream ends then too. It has `Path=/` (sent to `/monitor` and `/api/`),
  `HttpOnly` and `SameSite=Strict`, and `Secure` with `--secure-cookie` or behind a
  proxy that sends `X-Forwarded-Proto: https`. Signing out clears it in the browser;
  to invalidate every issued cookie at once, change the admin token and restart.
- **Response headers:** a strict CSP (`default-src 'self'`, no inline script,
  `frame-ancestors 'none'`), plus `nosniff` and `no-referrer`.
- **Gufo itself:** the monitor changes Gufo only through the keys file: with the admin
  token, or, with `--billing-config`, for portal keys as balances and accounts change.
  Every other route is read-only.

To reach the pages from elsewhere, put the listener behind a TLS reverse proxy or tunnel
([Cloudflare tunnel](#cloudflare-tunnel)); the portal is then public and `/monitor`
stays behind the admin sign-in. Keep Gufo's own port private unless you route its API.

## Tests

```sh
node tests/tools/monitor_icons_test.js
node tests/tools/monitor_auth_test.js
python3 tests/tools/monitor_test.py
python3 tests/tools/billing_test.py
python3 tests/tools/billing_chain_test.py
```
