# ytm-dlp

A Python script for downloading YouTube Music albums and playlists with configurable audio formats, metadata, and embedded square artwork. Built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org/).

---

## Features

- AAC downloads by default: Premium → format `141` (256kbps), Standard → format `140` (128kbps)
- Supports Opus downloads using yt-dlp format `251`
- Supports `m4a`, `ogg`/`.opus`, and `webm` containers, with codec-specific defaults
- Automatically detects whether the URL is an **album** or a **mixed playlist** and handles artwork accordingly
  - **Albums** — fetches the album's own square artwork and embeds it into every track
  - **Playlists** — resolves artwork per track in tiers (see [How artwork is chosen](#how-artwork-is-chosen))
- Artwork is always sourced from square thumbnails, with configurable requested resolution (600×600px by default)
- WebM output skips embedded artwork because the attached-artwork workflow is not supported
- Embeds artwork into M4A files (attached picture) and into Ogg/Opus files (`METADATA_BLOCK_PICTURE`)
- Sets the `date` metadata field to the original release year (sourced from YouTube Music's `release_year` field, falling back to upload year)
- Sets the `track_number` field from the playlist index
- Clears the `genre` tag (YouTube Music only provides the useless generic value "Music")
- Skips re-downloading files that already exist
- Checks pre-existing files with ffprobe and re-embeds artwork if missing
- Cleans up orphaned `.temp.m4a` files left by interrupted yt-dlp runs
- Ctrl+C is handled cleanly: scratch artwork is removed and the exit code is 130
- Never overwrites or deletes files it did not create (your own `cover.jpg` is left alone)
- Validates every downloaded image before use, and never replaces an audio file with a failed embed
- Uses yt-dlp's built-in retry mechanism for transient network failures
- `--dry-run` mode to preview what would happen without downloading anything
- `--log` option to save a full run log to disk
- Permanent TOML configuration via `~/.ytm-dlp/config.toml`
- `--init-config` creates a fully documented configuration template
- CLI options override configuration-file values for the current run
- `--format` option to override the codec/quality mapping
- `--ts` option to download only specific tracks by number or range
- `--dir` option to download into a target folder, creating it with confirmation if it doesn't exist
- `--cd` option for multi-disc downloads, splitting tracks into separate subfolders
- Multi-job support: download multiple albums/playlists in a single command, each to its own folder
- All target directories are checked and created upfront before any download begins
- Coloured terminal output throughout

---

## Requirements

The script is cross-platform. It uses Python, the `yt-dlp` Python package, `ffmpeg`, `ffprobe`, `colorama`, and Deno. The installation commands differ slightly by operating system.

### Python 3.11 or later

Python 3.11+ is recommended because the script uses the standard-library `tomllib` module for its TOML configuration file. `tomllib` was added in Python 3.11.

#### Windows

Install Python from [python.org](https://www.python.org/downloads/). During installation, enable **"Add Python to PATH"**.

Verify:

```powershell
python --version
```

#### macOS

If you use [Homebrew](https://brew.sh/), the simplest route is:

```bash
brew install python
```

Then verify:

```bash
python3 --version
```

Alternatively, install Python 3.11+ from [python.org](https://www.python.org/downloads/).

#### Linux

Install Python 3.11+ using your distribution's package manager, or use the packages from [python.org](https://www.python.org/downloads/) if your distribution provides an older version.

For example, on Debian/Ubuntu systems with a suitable Python package available:

```bash
sudo apt update
sudo apt install python3 python3-pip
```

Verify:

```bash
python3 --version
```

> **Python 3.10:** The script can also use the `tomli` backport when `tomllib` is unavailable. Install it with `python -m pip install tomli` (Windows) or `python3 -m pip install tomli` (macOS/Linux). Python 3.11+ is preferred.

### yt-dlp (Python package)

The script imports `yt_dlp` directly, so the Python package is required. The standalone `yt-dlp` executable is **not required by the script itself**.

#### Windows

```powershell
python -m pip install -U yt-dlp
```

#### macOS / Linux

```bash
python3 -m pip install -U yt-dlp
```

The official yt-dlp documentation also supports installing the package with pip on all three platforms.

Verify:

```text
# Windows
python -c "import yt_dlp; print(yt_dlp.version.__version__)"

# macOS / Linux
python3 -c "import yt_dlp; print(yt_dlp.version.__version__)"
```

> You may still install the standalone `yt-dlp` executable if you want the `yt-dlp` command available independently of this script. The official project provides binaries and package-manager installations for Windows, macOS, and Linux.

### ffmpeg and ffprobe

Both must be installed and available in your `PATH`.

#### Windows

```powershell
winget install Gyan.FFmpeg
```

#### macOS

With Homebrew:

```bash
brew install ffmpeg
```

#### Debian / Ubuntu

```bash
sudo apt update
sudo apt install ffmpeg
```

Other Linux distributions should use their normal package manager or a suitable build. FFmpeg's official download page lists packages and builds for Linux, Windows, and macOS.

Verify:

```text
ffmpeg -version
ffprobe -version
```

### colorama

Python library used for coloured terminal output. It works on all three platforms.

#### Windows

```powershell
python -m pip install -U colorama
```
#### macOS / Linux

```bash
python3 -m pip install -U colorama
```

### mutagen

Required **only** for artwork embedding in Ogg/Opus (`.opus`) output. The Ogg
container cannot carry an attached picture the way M4A does — ffmpeg rejects it
outright — so the picture is written as a base64 `METADATA_BLOCK_PICTURE`
Vorbis comment instead. M4A and WebM output do not need it. If it is missing,
opus downloads still work and artwork is simply skipped with a warning.

#### Windows

```powershell
python -m pip install -U mutagen
```

#### macOS / Linux

```bash
python3 -m pip install -U mutagen
```

### Deno

yt-dlp uses Deno as a JavaScript runtime for YouTube's challenge-solving components. Deno supports Windows, macOS, and Linux on both x64 and ARM64.

#### Windows

```powershell
winget install DenoLand.Deno
```

Or use Deno's official PowerShell installer:

```powershell
irm https://deno.land/install.ps1 | iex
```

#### macOS

With Homebrew:

```bash
brew install deno
```

Or use Deno's official shell installer:

```bash
curl -fsSL https://deno.land/install.sh | sh
```

#### Linux

The official shell installer is the simplest distribution-independent option:

```bash
curl -fsSL https://deno.land/install.sh | sh
```

Deno's documentation notes that its Linux distribution packages are community-maintained and may lag behind the current release, so the official installer is preferable when you want the current version.

Verify on all platforms:

```text
deno --version
```

Restart your terminal (or reload your shell configuration) if the command is not immediately found after using the installer.

### yt-dlp remote challenge solver

Deno alone is not sufficient: yt-dlp also needs its EJS challenge-solver component. For the Python package used by this script, initialize the solver cache once with:

#### Windows

```powershell
python -m yt_dlp --remote-components ejs:github "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

#### macOS / Linux

```bash
python3 -m yt_dlp --remote-components ejs:github "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

This downloads a short video as a side effect; delete it afterward.

> **Standalone yt-dlp:** If you also installed the standalone executable and intend to use it separately, initialize its own cache too by running `yt-dlp --remote-components ejs:github "https://www.youtube.com/watch?v=dQw4w9WgXcQ"`. The standalone executable and Python package can maintain separate caches.

### Browser cookies

The script uses `yt-dlp`'s browser-cookie support to authenticate with YouTube Music. The default browser is Firefox, but it can be changed with `--browser` or in the configuration file.

You must be logged into your YouTube Music account in the selected browser.

---

## Installation

### Windows

1. Place `ytm-dlp.py` somewhere permanent, for example:

   ```text
   C:\Users\YourName\Scripts\ytm-dlp.py
   ```

2. Add a function to your PowerShell `$PROFILE` so you can call it from any directory:

   ```powershell
   function ytm-dlp { python "$HOME\Scripts\ytm-dlp.py" @args }
   ```

3. If your PowerShell profile does not exist yet, create/edit it with:

   ```powershell
   notepad $PROFILE
   ```

4. Reload your profile or open a new terminal window.

### macOS

1. Put `ytm-dlp.py` somewhere permanent, for example:

   ```text
   ~/Scripts/ytm-dlp.py
   ```

2. Make it executable:

   ```bash
   chmod +x ~/Scripts/ytm-dlp.py
   ```

3. Create a convenient command in `~/.local/bin`:

   ```bash
   mkdir -p ~/.local/bin
   ln -sf "$HOME/Scripts/ytm-dlp.py" ~/.local/bin/ytm-dlp
   ```

4. Make sure `~/.local/bin` is on your `PATH`. For zsh (the default macOS shell), add this to `~/.zshrc` if necessary:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

5. Reload the shell:

   ```bash
   source ~/.zshrc
   ```

You can then run:

```bash
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..."
```

### Linux

1. Put `ytm-dlp.py` somewhere permanent, for example:

   ```text
   ~/Scripts/ytm-dlp.py
   ```

2. Make it executable:

   ```bash
   chmod +x ~/Scripts/ytm-dlp.py
   ```

3. Create a convenient command in `~/.local/bin`:

   ```bash
   mkdir -p ~/.local/bin
   ln -sf "$HOME/Scripts/ytm-dlp.py" ~/.local/bin/ytm-dlp
   ```

4. Make sure `~/.local/bin` is on your `PATH`. For Bash:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

   Add that line to `~/.bashrc` to make it permanent, then reload:

   ```bash
   source ~/.bashrc
   ```

   If you use zsh, put the same `export` line in `~/.zshrc` instead.

You can then run:

```bash
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..."
```

> **Alternative:** You can always invoke the script directly without creating a command shortcut: `python3 ~/Scripts/ytm-dlp.py ...`.

---

## Configuration file

The script can store permanent preferences in:

```text
~/.ytm-dlp/config.toml
```

On Windows this normally resolves to `C:\Users\YourName\.ytm-dlp\config.toml`. The file is optional; if it does not exist, built-in defaults are used.

### Creating the config file

Run:

```powershell
ytm-dlp --init-config
```

This creates a commented TOML template documenting every supported setting. If the file already exists, the script asks before overwriting it.

### Configuration precedence

For settings available on the command line, the precedence is:

1. **CLI option** — highest priority; applies only to the current run
2. **Config file** — permanent preference
3. **Built-in default** — used when neither is specified

For example, with:

```toml
[download]
codec = "opus"
quality = "premium"
browser = "firefox"
```

this command uses the configured Opus setting:

```powershell
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..."
```

while this overrides it only for the current run:

```powershell
ytm-dlp --codec aac "https://music.youtube.com/playlist?list=OLAK5uy_..."
```

### Config settings

#### `[download]`

| Setting | Default | Description |
|---|---|---|
| `browser` | `"firefox"` | Browser used for YouTube cookies. Supported names include `firefox`, `chrome`, `chromium`, `edge`, `brave`, `opera`, `safari`, `vivaldi`, and `whale`. |
| `codec` | `"aac"` | `aac` or `opus`. |
| `quality` | `"premium"` | AAC tier: `premium` = format `141`/256kbps; `standard` = format `140`/128kbps. Both Opus tiers use format `251`. |
| `container` | `""` | Empty uses the codec default: AAC → `m4a`, Opus → `ogg`. Opus may also use `webm`. |
| `format_override` | `""` | Raw yt-dlp format code. When set, bypasses the codec/quality mapping. |
| `retries` | `10` | Retry attempts for transient download failures. |

#### `[artwork]`

| Setting | Default | Description |
|---|---:|---|
| `size` | `600` | Requested square artwork size in pixels. YouTube Music natively serves up to 544px; larger requests may be server-side upscaled. |
| `max_workers` | `8` | Maximum concurrent thumbnail downloads for playlists. |
| `per_track` | `false` | `false` shares the playlist/album cover across tracks; `true` crops a distinct square from each track's largest video frame. See [How artwork is chosen](#how-artwork-is-chosen). |

#### `[behavior]`

| Setting | Default | Description |
|---|---|---|
| `log` | `"off"` | `"off"`, `"auto"`, or a fixed log-file path. `"auto"` creates a timestamped `ytm-dlp_YYYYMMDD_HHMMSS.log` file. |

The exact commented template can always be regenerated with `--init-config`. `--dry-run` is command-line-only and is not stored in the config file.

> **Python 3.10:** the script can use the `tomli` backport if `tomllib` is unavailable. Install it with `pip install tomli`. If TOML support is unavailable, an existing config is ignored and built-in defaults are used.

---

## Usage

Navigate to the folder where you want the files saved, then run:

```
ytm-dlp <URL> [per-job options]
```

Global options (apply to all jobs) go **before** the first URL. Per-job options follow each URL.

### Supported URL formats

| Type | Example URL pattern |
|---|---|
| Album (browse) | `https://music.youtube.com/browse/MPREb_...` |
| Album (playlist) | `https://music.youtube.com/playlist?list=OLAK5uy_...` |
| Mixed playlist | `https://music.youtube.com/playlist?list=PLxxxxx...` |

Albums are detected automatically by the `OLAK5uy_` prefix or `browse/MPRE` in the URL. Everything else is treated as a mixed playlist.

---

## Options

### Global options
These apply to every job and must be placed **before** the first URL.

| Option | Config key | Default | Description |
|---|---|---|---|
| `--format CODE` | `download.format_override` | none | Raw yt-dlp format override; bypasses codec/quality mapping. |
| `--codec aac\|opus` | `download.codec` | `aac` | Select audio codec. |
| `--quality premium\|standard` | `download.quality` | `premium` | AAC quality tier. |
| `--container m4a\|ogg\|webm` | `download.container` | per codec | Override the output container. |
| `--browser NAME` | `download.browser` | `firefox` | Browser used for YouTube cookies. |
| `--size N` | `artwork.size` | `600` | Requested square artwork size in pixels. |
| `--max-workers N` | `artwork.max_workers` | `8` | Concurrent playlist artwork downloads. |
| `--per-track-art` | `artwork.per_track` | `false` | Crop a distinct square per track instead of sharing the playlist cover. |
| `--retries N` | `download.retries` | `10` | Download retry count. |
| `--dry-run` | — | off | Preview without downloading or modifying files. |
| `--log [FILE]` | `behavior.log` | `off` | Write a log; without a filename, creates an automatic timestamped filename. |
| `--init-config` | — | — | Create the documented config template and exit. |

### Per-job options
These follow each URL and only apply to that download.

| Option | Default | Description |
|---|---|---|
| `--dir PATH` | cwd | Target directory. Supports nested paths using `/` or `\`. Missing folders prompt for confirmation before any download starts. |
| `--ts RANGE` | off | Download only specific tracks. Accepts comma-separated numbers and ranges, e.g. `"1, 3-6, 8-13"`. Cannot be combined with `--cd`. |
| `--cd FOLDER:RANGE` | off | Multi-disc spec. Creates a subfolder under `--dir` (or cwd) and downloads the given track range into it. Repeat for each disc. Cannot be combined with `--ts`. |

---

## Audio formats and containers

The normal codec/quality mapping is:

| Codec | Quality | yt-dlp format | Default container |
|---|---|---:|---|
| AAC | premium | `141` | `.m4a` |
| AAC | standard | `140` | `.m4a` |
| Opus | premium | `251` | `.opus` |
| Opus | standard | `251` | `.opus` |

For Opus + Ogg, the native Opus stream is remuxed into an Ogg container; it is not re-encoded. Opus + WebM keeps the native delivery container and skips artwork embedding. Invalid codec/container combinations fall back to the codec's default container with a warning.

Artwork is embedded differently per container:

| Container | Method | Requires |
|---|---|---|
| `.m4a` | ffmpeg attached picture | ffmpeg |
| `.opus` (Ogg) | `METADATA_BLOCK_PICTURE` Vorbis comment | mutagen |
| `.webm` | not supported — artwork is skipped | — |

When `--format` or `format_override` is used, the codec/quality mapping is bypassed.

### Output filenames

Playlist entries are zero-padded so they sort numerically:

```
03 - Song Title.m4a      # playlist entry 3
Song Title.m4a          # a bare video URL (no playlist index)
```

> **Note:** earlier versions used `3 Song Title.m4a`, and a bare video URL
> produced `NA Song Title.m4a`. If you already have a library built with the
> old names, those files will be downloaded again under the new names.

### How artwork is chosen

YouTube Music does something worth knowing: **it publishes square artwork for
the album/playlist, but not for individual tracks.** A track's own thumbnail
list is entirely 16:9 (typically 42 sizes up to 1920×1080), and yt-dlp exposes
no square variant — so "fetch the square thumbnail for this track" is not
possible; the data is not there.

Artwork is therefore resolved in tiers:

1. **The track's own square thumbnail**, when the source provides one (other
   extractors do).
2. **The playlist/album cover**, shared by every track. This is the normal
   YouTube Music path, and why an album download gets the real cover art.
3. **A distinct square per track**, cropped from that track's largest video
   frame — opt-in via `--per-track-art` / `per_track = true`.

Tier 3 exists for mixed playlists, where one shared cover tells you nothing
about which track is which. The trade-off: it is a video still, not real cover
art. Enable it per run or in the config file:

```powershell
ytm-dlp URL --dir "Mixed Playlist" --per-track-art
```

```toml
[artwork]
per_track = true
```

YouTube also advertises thumbnail sizes that do not exist — an album's
1200×1200 `maxresdefault` entry carries no signature and returns 404 — so
candidates are always tried largest-first and it falls through to the next one
(640×640 in practice). `--size` is therefore a request, not a guarantee: album
art arrives at YouTube's native resolution.

### `--format` and the output container

`--format` bypasses the codec/quality mapping, so the container can no longer be
derived from it. ytm-dlp infers the container from the format id instead
(`140`/`141` → m4a, `249`/`250`/`251` → webm, and so on):

```powershell
ytm-dlp URL --format 251          # container inferred as webm, artwork skipped
ytm-dlp URL --format 140          # container inferred as m4a, artwork embedded
ytm-dlp URL --format bestaudio    # unknown -> warns; pass --container to match
```

If the prediction is wrong, the run detects the real extension after
downloading and adapts rather than reporting an empty result.

---

## Examples

### Basic downloads

```powershell
# Download an album into the current directory
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..."

# Download into a specific folder (prompts to create if missing)
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "The Beatles\Revolver"

# Nested folder — prompts for each missing level individually
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "Rock\The Beatles\Revolver"

# Download only tracks 1 through 12
ytm-dlp "https://music.youtube.com/playlist?list=PLxxxxx..." --ts "1-12"

# Download tracks 1, 3 through 6, and 8 through 13
ytm-dlp "https://music.youtube.com/playlist?list=PLxxxxx..." --ts "1, 3-6, 8-13"
```

### Multi-disc albums

Use `--cd` when a single album spans multiple folders (e.g. a double album or box set). Each `--cd` spec creates a subfolder and downloads the specified tracks into it.

```powershell
# Double album split into two folders
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." `
    --dir "The Beatles\The White Album" `
    --cd "CD1:1-17" --cd "CD2:18-30"

# Box set with three discs, no base --dir (creates CD folders in cwd)
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." `
    --cd "Disc 1:1-20" --cd "Disc 2:21-40" --cd "Disc 3:41-55"
```

The `--cd` format is always `"FolderName:track-range"`. The folder is created automatically — no confirmation prompt.

### Multiple jobs in one command

In PowerShell, a comma between arguments creates an array, which Python receives as separate arguments. This lets you chain multiple download jobs in a single command. All target directories are checked and confirmed **before any download begins**.

```powershell
# Two albums, each to its own folder
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "The Beatles\Revolver", `
        "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "The Beatles\Abbey Road"

# Mix of album and playlist with different options
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "Beatles\White Album" --cd "CD1:1-17" --cd "CD2:18-30", `
        "https://music.youtube.com/playlist?list=PLxxxxx..." --dir "Beatles\Let It Be" --ts "1-12"

# Global flag applies to all jobs — place it before the first URL
ytm-dlp --format 140 `
        "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "Artist\Album1", `
        "https://music.youtube.com/playlist?list=OLAK5uy_..." --dir "Artist\Album2"
```

> **PowerShell tip:** The backtick `` ` `` is PowerShell's line continuation character. Long commands can be broken across lines for readability without affecting how they execute.

### Other options

```powershell
# Dry run — see exactly what would happen without downloading anything
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --dry-run

# Write a log file with an auto-generated timestamped name
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --log

# Write a log file with a specific name
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --log "abbey_road.log"

# Use native artwork resolution and increase retry count
ytm-dlp "https://music.youtube.com/playlist?list=OLAK5uy_..." --size 544 --retries 20
```

---

## Directory prompts

When `--dir` specifies a path that doesn't fully exist, the script pauses and asks for each missing component **before starting any download**. For multi-job commands, all directories across all jobs are checked together upfront.

Each missing folder presents three choices:

| Key | Action |
|---|---|
| `Y` | Create the folder with the given name and continue |
| `N` | Cancel the entire operation — nothing is downloaded |
| `M` | Modify — enter a different name for this folder, then continue |

Example prompt:
```
[WARN] Directory does not exist: D:\Music\The Beatles\Revolver
  Create 'Revolver'? [Yes / No / Modify name]
```

---

## Re-running on an existing folder

It is safe to run the script again in a folder where files have already been downloaded. The script will:

1. Skip re-downloading any tracks already present (yt-dlp's default behavior)
2. Use ffprobe to check each existing file for embedded artwork
3. Only mux artwork into files that are missing it
4. Print `All files already have artwork — nothing to mux.` if everything is already complete

This makes it safe to re-run after a partial download, a failed artwork embed, or to repair files that were downloaded without artwork by a previous version of the script.

---

## Updating yt-dlp

When a new yt-dlp release is out, update both installations:

```
yt-dlp -U
pip install -U yt-dlp
```

The remote challenge solver script does **not** need to be re-downloaded on every yt-dlp update — it is versioned and cached independently.

---

## Troubleshooting

### `Requested format is not available`
This usually means the JS challenge solver is not working. Check:
1. Deno is installed and in PATH: `deno --version`
2. The remote solver has been downloaded for **both** the exe and pip package (see setup step 6)

### `HTTP Error 429: Too Many Requests`
YouTube is rate-limiting your IP. Wait a few minutes and try again. This is unrelated to the script.

### `[WinError 32] The process cannot access the file`
Windows Defender or the Search indexer locked a file during yt-dlp's temp rename. The script retries locked rename/replace operations and removes leftover `*.temp.<extension>` files before the artwork/mux phase, so re-running is safe.

### Artwork appears as 16:9 instead of square
This should not happen with the current script — it explicitly filters for square thumbnails (`width == height`) before downloading. If you encounter this, it means no square thumbnail was available for that track and it was skipped.

### Files from a previous run have no artwork
Re-run the script in the same folder. The ffprobe check will identify any files missing artwork and mux them on the next pass.

### Date tag shows a recent year instead of the original release year
The script uses YouTube Music's `release_year` field, which reflects the original release date displayed in the app. If you see an unexpectedly recent year, it means YouTube Music itself has that year stored for that release — this sometimes happens with reissues or re-uploads.

### Multi-job command only runs one job
Make sure the comma between jobs has no space before it and that you are running in PowerShell (not Command Prompt). The comma is PowerShell array syntax and is what splits the jobs. In Command Prompt this syntax does not work.
