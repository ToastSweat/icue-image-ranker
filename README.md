# iCUE Image Ranker

A native CORSAIR iCUE widget for **XENEON EDGE** that learns your favorite local images and videos through pairwise comparisons.

- Two media items stay on screen until you choose the better one.
- Votes, Elo ratings, and the unanswered pair persist locally across restarts.
- A provisional **Top 100** view shows your current rated favorites.
- MP4/WebM playback is always muted; large images get monitor-sized streaming previews.
- Native iCUE settings control visibility schedules, text, button size, image fit, colors, transparency, and video playback.

The repository contains **source code and safe templates**, not an image collection, personal rankings, installed dependencies, or preconfigured packages.

## Works alongside r34-tag-watcher

This project is designed to work in tandem with [ToastSweat/r34-tag-watcher](https://github.com/ToastSweat/r34-tag-watcher):

1. Set up and run the watcher using that repository's own instructions.
2. Point Image Ranker's `archive_root` at the watcher's output archive directory.
3. Keep both applications running independently. The ranker discovers completed downloads during its periodic scans.

The integration is **through files**, not the watcher's database or a special API. Image Ranker reads the archive without modifying its files, downloader settings, processes, or `.r34-watcher` state. It also works with another existing local media folder if you do not use the watcher.

Tag-organized subfolders and **NTFS hard links** are supported. Identical filenames across tag directories share one ranking identity. Different filenames are not deduplicated by visual similarity or content hash. Source media is never renamed, moved, resized in place, or deleted.

## Requirements

- 64-bit Windows and a XENEON EDGE display.
- **iCUE 5.47 or newer** for custom widgets.
- **Python 3.11 or newer**.
- **Node.js and npm**; Node.js 22+ is recommended.
- **FFmpeg and ffprobe on PATH** for video still previews and the media tests. Use an FFmpeg build with H264/libx264 and AAC support. See [FFmpeg downloads](https://ffmpeg.org/download.html).
- Git if cloning the repository.

Python and npm dependencies are installed locally. Do not copy someone else's `.venv`, `node_modules`, `config.json`, or packaged widget.

## Set up from a fresh clone

The commands below are for **PowerShell**, run in the repository directory. Replace the example archive path with an **existing** folder containing your media.

```powershell
git clone https://github.com/ToastSweat/icue-image-ranker.git
cd icue-image-ranker

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci --include=dev

npm run setup -- --archive-root "C:\Media\Archive"
npm run validate
npm run package
```

If the Python launcher `py` is unavailable, use `python -m venv .venv` instead. No virtual-environment activation is required.

`npm run setup`:

- creates an ignored **`config.json`** from `config.example.json`;
- generates a random private API key for this installation;
- creates ignored **`widget/defaults.js`** for the local browser preview;
- **preserves an existing configuration and API key** instead of resetting them.

If you already have `config.json`, rerun `npm run setup` without an archive argument to refresh generated defaults. To change existing settings, edit your private configuration rather than expecting setup to overwrite it.

### Build and import the native widget

`npm run package` creates:

```text
dist/Image Ranker.icuewidget
```

The build copies public widget templates into ignored **`build/widget/`**, injects your own endpoint/key there, and packages that private runtime using the official iCUE Widget CLI. It does not put your key into the public `widget/index.html` template.

**Use `npm run package`, not a direct package command against `widget/`.** The public templates intentionally contain no installation-specific key.

1. Open iCUE's **Widgets** panel.
2. Click **+** and import your generated `.icuewidget` file. Double-clicking the package can also start the import wizard.
3. Add **Image Ranker** to a XENEON EDGE slot. Both landscape and portrait layouts are supported; a large landscape slot is useful for comparisons.
4. Start the local companion:

   ```powershell
   npm start
   ```

   Alternatively, double-click **`start.cmd`**. Leave the companion running while using the widget. Run only one instance on a given port.

An initial scan of a large archive can take time. Once a pair is shown, later scans and polling do not discard it.

For browser preview, open `http://127.0.0.1:47831/` while the companion is running. If you change the configured port, use that port instead.

An imported widget is a snapshot. After changing widget code or connection defaults, rebuild and import the new package, then use that version in your slot. Changing source files does not update an already imported copy.

## Compare, rank, and display favorites

Tap the better image or video. The vote is saved before the next pair appears; choices remain disabled until both items load. Duplicate taps are guarded, and a lost response can be reconciled against a durable vote receipt without saving the vote twice.

Ratings use **Elo**, starting at 1000 with update factor 32. Selection mixes less-rated candidates with strong contenders. Top 100 contains **rated** items only and remains provisional while you explore the collection. It is not a claim that the entire archive's absolute best 100 have been found.

Top 100 is paged **five items at a time**. Previous/Next controls browse the list without rating anything or changing the pending comparison. Only the current page's media is loaded, so the widget never plays 100 videos simultaneously. Existing native `top5` view settings remain compatible internally; the visible view is now Top 100.

### Less repetition

Selection uses persisted comparison history, not just the immediately previous pair. Exact unordered pairs have a configurable cooldown, recently shown individual images get a rest, and refinement considers a broader ranked pool instead of repeatedly comparing only the same leading images. The discovery preference defaults to `0.9` so the rest of a large archive continues to enter comparisons.

Cooldowns are best-effort: a two-image or heavily filtered library must eventually repeat. They relax only when needed rather than leaving the ranker with no usable pair. Changing the selection policy never resets existing ratings/history or discards the unanswered pair.

### Phone access

See [PHONE.md](PHONE.md) for the optional one-time-code-paired browser gateway. The PC remains the authority for all votes and media; the core API stays loopback-only and its private key is never sent to the phone. LAN HTTP is unencrypted, so use trusted home Wi-Fi only and never port-forward it to the Internet. Network/firewall changes are opt-in, not part of normal setup or startup.

### When an item will not load

**Retry and Skip pair are always present for a visible comparison**, including while media is still loading. They do not depend on a browser error event, a status-text setting, or a successful image/video decode:

1. **Retry** reloads the same comparison and checks the current server state. Use this after a temporary connection problem or after correcting the source.
2. **Skip pair** is an explicit manual escape from a comparison, including one that has stalled without reporting an error. It records an abstention, **not a vote**, leaves Elo/match counts unchanged, and never deletes either source file.
3. The companion checks reported failed items before excluding them. Confirmed unreadable/missing/out-of-budget sources stay out of future selection until a later scan detects their size or modification time changed. Healthy files are not excluded just because a browser failed to display them.

Errors do not automatically advance a pair. A 15-second media-load watchdog marks stalled loads and attempts a real video-poster fallback; manual Skip is available before that deadline. JSON requests also have finite deadlines so a stalled save cannot disable recovery forever. Recovery controls work as icons when text is disabled. Lost skip responses are reconciled against a durable receipt, and web/iCUE clients can synchronize a pair already voted or skipped in another client without inventing a vote.

The current widget is visibly named **Image Ranker 1.5** in iCUE and shows **v1.5.0** beside its title when title text is enabled. Build/import this version and replace older widget instances; updating the companion does not rewrite an already imported iCUE HTML snapshot. The 1.4 video preview probe reads the top-level JSON video stream rather than assuming FFprobe CSV has exactly one row, which matters for valid MP4 stream groups.

Some archives contain a valid image with a misleading filename extension (for example, bitmap content under a `.jpg` name). If the primary streaming decoder cannot read a small, valid raster format, a bounded Pillow fallback reads the actual content. This does not rename, rewrite, or copy the original archive file.

### Images and videos

- Raster image formats are configured separately from video formats.
- Large-image previews use libvips streaming, not a full-size copy sent to the monitor. Originals remain unchanged.
- GIFs currently use a still preview.
- MP4/WebM videos can appear in comparisons and Top 100. They are **muted, volume zero, with no unmute controls**.
- Playback loops by default and never advances a pair. **Play videos** and **Loop videos** can be disabled independently in iCUE.
- Unsupported browser codecs can fall back to a clearly labeled real first-frame preview when FFmpeg can decode the source. Not every codec inside an MP4/WebM container is guaranteed to play in iCUE/Qt.
- Hiding the widget, leaving the allowed schedule, switching views, or replacing a rated pair pauses playback and removes media sources. Polling preserves the current unanswered clip.
- Incomplete `.part` files and recently changing downloads are not selected until settled.

## Native display settings

### Visibility schedule

Scheduling is **off by default**. For weekdays from 8am to 3pm:

- enable the schedule;
- enable Monday–Friday and disable Saturday/Sunday;
- set start to `08:00` and end to `15:00`.

Times use the computer's local clock in 24-hour `HH:MM` format. Start is inclusive, end exclusive. Overnight periods belong to their selected start day: Monday `22:00`–`02:00` includes early Tuesday. Equal start/end means all day on enabled days. Invalid times hide media rather than guessing.

The schedule also applies to Top 100. It hides this widget's media, **not** the monitor backlight, iCUE wallpaper, or other widgets. Indexing may continue while the display is hidden.

The on-screen **Hide/Show** button is a temporary session override. The native **Hide images** switch is the persisted setting. Browser preview does not persist native iCUE sidebar settings.

### Text, buttons, and personalization

- **Show text** is a master switch. Individual switches control the title, status, Top 100 captions, button labels, and filenames. With text off, action buttons use icons.
- **Button size** adjusts Top 100/Compare and Hide/Show controls from **44–144 px**, including labels/icons. Default is **72 px**; try **120 px** on a large slot. Image vote targets fill the remaining area automatically.
- **Image fit** selects contain or cover.
- Background transparency applies without fading the media, text, or icons. Following iCUE's opacity mapping, `0` is transparent and `100` is opaque.
- Enable iCUE's **Custom Style** if device-wide styling overrides the widget's chosen colors.

## Companion configuration

Stop the companion before editing `config.json`, then restart it. Native display settings update live; companion settings require a restart.

| Key | Purpose |
| --- | --- |
| `archive_root` | Existing absolute path to the read-only media archive |
| `port` | Loopback port, default `47831` |
| `api_token` | Installation-specific private key generated by setup |
| `scan_interval_seconds` | Discovery interval, default `3600` |
| `settle_seconds` | Required file age before discovery, default `120` |
| `extensions` | Raster image suffixes; do not put MP4/WebM here |
| `excluded_dirs` | Folders excluded from scans; retain watcher/project exclusions |
| `include_dirs` | Optional archive-relative folders; `[]` means the full archive |
| `exploration_fraction` | `0`–`1`, default `0.9`; higher emphasizes less-rated candidates |
| `pair_repeat_cooldown` | Avoid an unordered pair from the recent `500` comparisons when alternatives exist |
| `image_repeat_cooldown` | Rest individual images shown in the recent `12` comparisons when alternatives exist |
| `refinement_pool_size` | Broader ranked refinement pool, default `100` instead of just the top ten |
| `thumbnail_max_pixels` | Maximum preview side length, default `1200` |
| `max_source_pixels` | Source image budget, default `500000000` |
| `max_source_bytes` | Image source-file budget, default `268435456` (256 MiB) |
| `include_videos` | Discover supported video files, default `true` |
| `video_extensions` | Video suffixes, default `.mp4` and `.webm` |
| `max_video_bytes` | Streamed-video source-size budget, default 8 GiB |

For example, `"include_dirs": ["Artist/Example", "Character/Example"]` restricts selection to those existing folders. Use actual archive-relative folder names, not absolute paths or `..`. Historical rankings are retained for excluded items. Do not disable a pending video's source and expect the comparison to be silently skipped; the unanswered pair is preserved intentionally.

After changing the port or key, rerun setup and rebuild the package. Update the installed widget's connection settings to match, or import and configure the new build.

## Local data, privacy, and Git hygiene

The local database is **`data/rankings.sqlite3`**. Back up `data/` and `config.json` with the companion stopped. An authenticated, read-only `/api/export` endpoint exports rankings and vote history as JSON.

The server binds only to `127.0.0.1`, requires authentication for media reads and votes, and prevents another companion from binding the same port on Windows. It is a local application, not a security boundary against malicious processes running as the same Windows user. There is no media deletion endpoint.

### Never publish these files

`.gitignore` excludes:

- private `config.json` and generated `widget/defaults.js`;
- ranking databases, SQLite sidecars, backups, exports, logs, and local verification reports;
- downloaded media and generated test clips;
- `.venv`, `node_modules`, caches, and installed binary libraries;
- `build/`, `dist/`, and **all `.icuewidget` files**;
- local implementation notes and machine-specific launchers.

**Generated packages embed your local API key. Do not upload them to GitHub Releases, attach them to issues, or share them as public binaries.** Share this source repository so each person builds a package with their own key.

Safe files intended for Git include source code, `config.example.json`, `widget/defaults.example.js`, `requirements.txt`, `package.json`, and **`package-lock.json`**. Dependency manifests/lockfiles belong in Git; installed dependencies do not.

Before committing, review:

```text
git status --short
git add --dry-run .
git diff --cached
```

Git ignore rules do not remove files already tracked or erase older commits. If a secret was previously committed or published, remove it from tracking/history as appropriate **and rotate the key**. Do not use forced staging to add ignored local data. Deliberately authored assets under `widget/resources/` should also be reviewed before publication.

## Tests and development

After local setup:

```powershell
npx playwright install chromium
npm run fixtures
npm test
npm run validate
npm run package
```

Test clips are generated from a solid color and a sine-wave audio track; no downloaded archive media is used. `npm test` generates the clip if missing and preserves an existing fixture. Tests use isolated temporary databases and media rather than casting votes against the configured archive.

Checks cover persistence, hard links, large-image previews, authenticated video byte ranges/posters, muted playback, schedule privacy, text/button settings, background compositing, all eight EDGE slot sizes, XML-safe metadata, and public-template/private-build separation.

Browser simulation and CLI validation are not proof of physical-device codec support or touch behavior. Test the imported widget on your own display.

Official widget documentation: [Getting started](https://docs.elgato.com/icue/widgets/) and [Specification](https://docs.elgato.com/icue/widgets/specification).
