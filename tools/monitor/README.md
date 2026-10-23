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
- optionally, the server's named API keys with their usage, which an admin can
  create, edit and revoke ([API keys](#api-keys)).

One Python process (standard library only, Python 3.10 or later) polls Gufo, sysfs and
`/proc` once per second and serves the page. Any number of browsers share those polls:
`GET /api/state` returns a snapshot with history, and `GET /api/stream` sends one
Server-Sent Event per poll. The page has no build step and loads nothing from other hosts.

## Run

```sh
python3 tools/monitor/gufo-monitor.py --gufo http://127.0.0.1:8080
```

Open `http://127.0.0.1:8099/` and log in with the monitor token. If the token file
does not exist, the monitor creates it on first start at
`$XDG_CONFIG_HOME/gufo-monitor/token` with mode 0600. Scripts can send the token as
`Authorization: Bearer <token>`.

| Option | Default | Meaning |
|---|---|---|
| `--gufo URL` | `http://127.0.0.1:8080` | Gufo base URL |
| `--gufo-key-file FILE` | none | file holding the server's `--api-key`; read and used only by the monitor |
| `--bind`, `--port` | `127.0.0.1`, `8099` | listen address |
| `--token-file FILE` | `$XDG_CONFIG_HOME/gufo-monitor/token` | monitor token, created if missing |
| `--log-source` | `none` | `podman:CONTAINER`, `file:PATH` or `none` |
| `--log-since` | `1h` | podman log backlog read at start |
| `--log-utc` | | `file:` log timestamps are UTC (Gufo inside a container) |
| `--poll`, `--history` | `1`, `900` | poll interval and sparkline window, seconds |
| `--gpu-card` | `auto` | DRM card, for example `card1` |
| `--show-clients` | | show client ids from the log |
| `--secure-cookie` | | always mark the login cookie `Secure` |
| `--keys-file FILE` | none | the server's `--api-key-file`; enables the API keys section |
| `--admin-token-file FILE` | `$XDG_CONFIG_HOME/gufo-monitor/admin-token` | admin token for changing keys, created if missing |

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
- **Telemetry:** from `/sys/class/drm/cardN/device` (`gpu_busy_percent` and
  `mem_info_gtt_*`), its `amdgpu` hwmon (temperature, power, clock), `/proc/meminfo` and
  `/proc/stat`.

## API keys

With `--keys-file`, the page lists the named keys of Gufo's `--api-key-file`. For each
key it shows its access, its models and its usage from the log: requests, prompt,
cached and generated tokens, errors and last use. It also reports whether Gufo
applied the latest version of the file.

After unlocking with the admin token, you can:

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

## Security

- **Gufo API key:** it stays in the monitor process. It never appears in a response,
  the page or the monitor's own log.
- **Monitor token:** every route except `/login` and `/static/*` requires it. Without
  it, API routes answer `401` and pages redirect to `/login`.
- **Login cookie:** it holds an HMAC of the token, not the token itself. It is `HttpOnly`
  and `SameSite=Strict`, and also `Secure` with `--secure-cookie` or behind a proxy that
  sends `X-Forwarded-Proto: https`.
- **Response headers:** a strict CSP (`default-src 'self'`, no inline script,
  `frame-ancestors 'none'`), plus `nosniff` and `no-referrer`.
- **Admin token:** only needed with `--keys-file`. Viewing keys and their usage takes
  the monitor token; creating, editing or revoking keys also takes the admin token,
  sent as `X-Admin-Token` or as an hour-long `/api/`-only cookie set by unlocking the
  page. Changes are JSON requests, so a cross-site form cannot submit them.
- **Gufo itself:** the monitor changes Gufo only through the keys file, and only with
  the admin token. Every other route is read-only.

To reach the monitor from elsewhere, put it behind a TLS reverse proxy or tunnel and
keep Gufo's own port private.

## Tests

```sh
python3 tests/tools/monitor_test.py
```
