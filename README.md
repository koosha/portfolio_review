# Portfolio Review

A local app that reads selected Yahoo Finance Holdings tables through a Chrome extension and saves them in SQLite. Yahoo and Google sign-in stay in your normal Chrome session. This phase collects data; it does not analyze investments or place trades.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Chrome. Then, from the repository folder:

```sh
uv sync
uv run app.py
```

Open [the local app](http://127.0.0.1:8765). Leave the terminal running.

The default data directory is `data/` inside the repository and is ignored by Git. To use an existing database and pairing key:

```sh
uv run app.py --data-dir /path/to/existing/data
```

`PORTFOLIO_DATA_DIR` can also set the data directory. Use `--port` to change the local port.

## Connect and pull

1. Open `chrome://extensions`, enable **Developer mode**, and choose **Load unpacked**. Select this repository's `chrome-extension` folder.
2. Open **Local Portfolio** from Chrome's Extensions menu. Copy the pairing key from the app's **Connection settings**, paste it into the extension, and save.
3. Click **Open Yahoo** and sign in normally. Click **Find portfolios** in the local app.
4. Check the portfolios you want, then click **Pull latest holdings**. Keep Chrome open while it runs.

Choices persist. Unchecked portfolios are excluded from pulls and exports; their previous snapshots remain available. New discoveries start unchecked.

Cards show market value and the last successful pull. **View holdings** opens the saved rows and history; **Capture details** contains completeness and missing-value notes. **Export holdings** exports selected data as JSON. The CSV export is generated locally from captured table cells; no Yahoo download button is required.

After an extension code update, reload Local Portfolio in `chrome://extensions`. For UI-only changes, refresh the app page. Restart the app after Python changes.

## Data and limitations

- `sources` stores portfolio identities, selections and pull status.
- `snapshots` retains captured rows, raw displayed fields and capture metadata.
- `positions` stores decimal share quantities, prices, cost and market value where supplied.
- `latest_positions` exposes the most recent saved positions for selected portfolios.

Successful changes create snapshots. Unchanged captures update the check time. Failed pulls preserve previous data.

Market value sums captured values and the Yahoo Total Cash balance within each explicit currency. Unknown currencies remain unspecified. Yahoo's **Add** prompt means no quantity was recorded; the original display remains in the snapshot and is not turned into a zero-share position. Purchase lots and history are not reconstructed.

The reader checks account identity, row widths, pagination and changing rows. If Yahoo exposes a total row count, the capture must match it. Otherwise it is marked **completeness unverified**. Compare the captured totals with Yahoo before using the data for analysis.

## Development

Python's standard library provides the server and SQLite storage. JavaScript has no runtime dependencies. Node.js 20+ is needed only for extension/UI tests.

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -q
node --test tests/*.test.mjs
```

Test fixtures are synthetic. Keep new tests independent of personal accounts.

## Local data and Git

The server binds only to loopback. App mutations require a local token; the extension uses a revocable pairing key and sends captured tables only to the configured loopback app. The extension cannot read Google pages or cookies.

`.gitignore` excludes the database, credentials, browser profiles, exports, virtual environments and local artifacts. Never force-add ignored personal files. Review `git diff --cached` before committing. The database and pairing key are not application-encrypted; keep them private and back them up separately while the app is stopped.

To retain the requested commit identity in this checkout:

```sh
git config user.name koosha
git config user.email koosha.g@gmail.com
```

## Structure

- `portfolio/server.py`: local HTTP routes and startup
- `portfolio/companion.py`: paired Chrome request queue
- `portfolio/storage.py`: validation, snapshots and SQLite storage
- `portfolio/yahoo.py`: portfolio-link normalization
- `chrome-extension/`: connection panel, worker and Holdings reader
- `static/`: local UI
- `tests/`: storage, HTTP, collector and UI tests

Read-only data APIs are `/api/export`, `/api/positions`, `/api/sources/{id}/snapshots` and `/api/snapshots/{id}`. Selection and collection use the authenticated local UI.
