#!/usr/bin/env python3
"""
ytm-dlp.py — YouTube Music downloader with per-track square artwork embedding.
Supports albums (OLAK5uy_ / browse/MPRE) and mixed playlists.

Config file:
  ~/.ytm-dlp/config.toml holds your permanent preferences (browser, codec,
  quality, artwork size, etc). Run `ytm-dlp --init-config` to create one
  with full documentation in comments. CLI flags always override the
  config file for that one run. Requires Python 3.11+ (tomllib built in),
  or `pip install tomli` on 3.10.

Multi-job usage (PowerShell — comma separates jobs):
  ytm-dlp URL1 --dir "Artist/Album1", URL2 --dir "Artist/Album2"

Single-job usage:
  ytm-dlp URL --dir "Artist/Album" --ts "1-12"

Multi-disc usage:
  ytm-dlp URL --dir "Artist/Album" --cd "CD1:1-17" --cd "CD2:18-30"

Output filenames:
  03 - Song Title.m4a      (playlist entries, zero-padded)
  Song Title.m4a          (a bare video URL)

Global flags (before first URL, apply to all jobs — all overridable via
config file, see --init-config):
  --format code       Raw yt-dlp format override (advanced; bypasses codec/quality)
  --codec aac|opus     Audio codec (default: aac)
  --quality premium|standard  AAC quality tier (default: premium)
  --container m4a|ogg|webm    Container override (default: per-codec)
  --browser NAME       Browser to pull cookies from (default: firefox)
  --size N             Artwork resolution px (default: 600)
  --max-workers N       Concurrent thumbnail downloads (default: 8)
  --per-track-art       Crop a distinct square per track instead of sharing
                        the playlist cover (useful for mixed playlists)
  --retries N          Download retry count (default: 10)
  --dry-run            Preview without downloading
  --log [FILE]         Write log file
  --init-config        Write a documented config template and exit
"""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Optional

import yt_dlp
from colorama import Fore, Style, init as colorama_init

# Optional: only needed to embed artwork into Ogg/Opus, where the Ogg muxer
# cannot carry an attached picture (ffmpeg rejects it outright). When mutagen
# is unavailable we degrade gracefully to skipping artwork for those files.
try:
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

# ---------------------------------------------------------------------------
# Windows File Locking Patch
# ---------------------------------------------------------------------------

_orig_os_replace = os.replace
_orig_os_rename  = os.rename

def _safe_os_replace(src, dst):
    for _ in range(20):
        try:
            return _orig_os_replace(src, dst)
        except PermissionError:
            time.sleep(0.5)
    return _orig_os_replace(src, dst)

def _safe_os_rename(src, dst):
    for _ in range(20):
        try:
            return _orig_os_rename(src, dst)
        except PermissionError:
            time.sleep(0.5)
    return _orig_os_rename(src, dst)

os.replace = _safe_os_replace
os.rename   = _safe_os_rename

colorama_init(autoreset=True)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path | None) -> logging.Logger:
    logger = logging.getLogger("ytm-dlp")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    if log_path:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        print_info(f"Logging to: {log_path}")
    return logger


# ---------------------------------------------------------------------------
# Coloured print helpers
# ---------------------------------------------------------------------------

def print_info(msg):        print(f"{Fore.CYAN}{msg}{Style.RESET_ALL}")
def print_ok(msg):          print(f"  {Fore.GREEN}[OK]   {Style.RESET_ALL}{msg}")
def print_skip(msg):        print(f"  {Fore.YELLOW}[SKIP] {Style.RESET_ALL}{msg}")
def print_fail(msg):        print(f"  {Fore.RED}[FAIL] {Style.RESET_ALL}{msg}")
def print_warn(msg):        print(f"  {Fore.YELLOW}[WARN] {Style.RESET_ALL}{msg}")
def print_art(msg):         print(f"  {Fore.CYAN}[ART]  {Style.RESET_ALL}{msg}")
def print_art_reuse(msg):   print(f"  {Fore.CYAN}[ART]  {Style.RESET_ALL}{msg} (reused)")
def print_temp(msg):        print(f"  {Fore.YELLOW}[TEMP] {Style.RESET_ALL}{msg}")
def print_success(msg):     print(f"{Fore.GREEN}{msg}{Style.RESET_ALL}")
def print_dry(msg):         print(f"{Fore.MAGENTA}[DRY-RUN] {Style.RESET_ALL}{msg}")
def print_header(msg):      print(f"\n{Fore.CYAN}{'─' * 60}\n  {msg}\n{'─' * 60}{Style.RESET_ALL}")


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".ytm-dlp" / "config.toml"

DEFAULT_CONFIG_TEMPLATE = """\
# ytm-dlp configuration file
#
# Set your permanent preferences here. Any setting can still be overridden
# per-run with the matching CLI flag (e.g. `--codec opus` for one download
# without changing this file). Lines starting with # are comments.

[download]
# Which browser to pull YouTube cookies from for authentication.
# Options: firefox, chrome, chromium, edge, brave, opera, safari, vivaldi
browser = "firefox"

# Audio codec to download.
#   aac  = AAC audio in an M4A container (most compatible; default)
#   opus = Opus audio (higher quality per bit)
codec = "aac"

# Audio quality tier. Only affects AAC downloads.
#   premium  = format 141, 256kbps (requires YouTube Music Premium)
#   standard = format 140, 128kbps (works without a subscription)
quality = "premium"

# Container override. Leave as "" to use the default container for the
# chosen codec:
#   aac  -> m4a   (.m4a)
#   opus -> ogg   (.opus — Opus audio in an Ogg container)
#
# For opus you may explicitly set "webm" instead of "ogg" to keep
# YouTube's native delivery container with no remuxing step.
# NOTE: cover art embedding is NOT supported for webm and will be
# automatically skipped if this is selected.
#
# Artwork for ogg/.opus files is stored as a METADATA_BLOCK_PICTURE
# Vorbis comment (written by mutagen), because the Ogg container cannot
# carry an attached picture the way M4A does. Install mutagen to enable
# it:  pip install mutagen
# Without mutagen, opus downloads still work but artwork is skipped.
container = ""

# Advanced: raw yt-dlp format code, e.g. "251". When set, this completely
# bypasses the codec/quality mapping above. Leave as "" unless you know
# the specific format code you want.
format_override = ""

# Number of retry attempts for transient network failures.
retries = 10

[artwork]
# Target artwork resolution in pixels (square). YouTube Music natively
# serves up to 544px; requesting higher causes a slight server-side upscale.
size = 600

# Max concurrent thumbnail downloads when fetching artwork for a playlist.
max_workers = 8

# How artwork is chosen for tracks that have no square thumbnail of their own
# (which is every YouTube Music track — YouTube only publishes square art for
# the album/playlist itself).
#   false = share the playlist/album cover across all tracks
#   true  = crop a distinct square out of each track's largest video frame.
#           Useful for mixed playlists, where one shared cover tells you
#           nothing about which track is which, but note the result is a video
#           still rather than real cover art.
per_track = false

[behavior]
# Default log behavior: "off", "auto" (timestamped filename), or a fixed
# file path, e.g. "ytm-dlp.log"
log = "off"
"""

KNOWN_BROWSERS = {
    "brave", "chrome", "chromium", "edge", "firefox",
    "opera", "safari", "vivaldi", "whale",
}


def load_config(path: Path) -> dict:
    if tomllib is None:
        if path.exists():
            print_warn(
                "Found a config file but Python's TOML support is unavailable. "
                "Run: pip install tomli  (needed on Python < 3.11). Using defaults."
            )
        return {}
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print_warn(f"Could not parse config file ({path}): {e}. Using defaults.")
        return {}


def cfg_get(config: dict, section: str, key: str, default):
    return config.get(section, {}).get(key, default)


def write_default_config(path: Path):
    if path.exists():
        print_warn(f"Config file already exists at: {path}")
        raw = input(
            f"  Overwrite it? [{Fore.GREEN}Y{Style.RESET_ALL}es / "
            f"{Fore.RED}N{Style.RESET_ALL}o] "
        ).strip().upper()
        if raw != "Y":
            print_info("Cancelled — existing config file left unchanged.")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    print_success(f"Config file created at: {path}")
    print_info("Edit it to set your permanent preferences.")


# ---------------------------------------------------------------------------
# Codec / container resolution
# ---------------------------------------------------------------------------

CODEC_FORMAT_MAP = {
    ("aac", "premium"):  "141",
    ("aac", "standard"): "140",
    ("opus", "premium"):  "251",
    ("opus", "standard"): "251",
}
CODEC_DEFAULT_CONTAINER = {"aac": "m4a", "opus": "ogg"}
VALID_CONTAINERS_FOR_CODEC = {"aac": {"m4a"}, "opus": {"ogg", "webm"}}
# Container -> actual file extension yt-dlp/ffmpeg will produce
CONTAINER_EXT = {"m4a": "m4a", "ogg": "opus", "webm": "webm"}

# How artwork gets embedded, keyed by the final file extension:
#   ffmpeg  - MP4/M4A carries an attached_pic video stream (verified working)
#   mutagen - Ogg/Opus cannot hold an mjpeg stream at all, so the picture has
#             to go into a METADATA_BLOCK_PICTURE Vorbis comment instead
#   skip    - WebM/Opus delivery containers are not supported
ARTWORK_BACKEND = {"m4a": "ffmpeg", "opus": "mutagen", "webm": "skip"}

# `--format` bypasses the codec/quality mapping, so the output container can no
# longer be inferred from codec/quality — it has to come from the format id.
# Without this, `--format 251` (Opus/WebM) still searched for *.m4a, matched
# nothing, and reported "All files already have artwork — nothing to mux".
FORMAT_ID_CONTAINER = {
    "139": "m4a",    # AAC ~48k
    "140": "m4a",    # AAC ~128k
    "141": "m4a",    # AAC ~256k
    "256": "m4a",    # AAC ~5k
    "258": "m4a",    # AAC ~384k
    "249": "webm",   # Opus ~50k
    "250": "webm",   # Opus ~70k
    "251": "webm",   # Opus ~160k
    "338": "webm",   # Opus ~480k
}
# Extensions we are willing to adopt if the predicted one turns out wrong.
AUDIO_EXTS = ("m4a", "opus", "ogg", "webm", "mp3", "mka")


def infer_container_from_format(fmt: str) -> str | None:
    """
    Best-effort container for a raw yt-dlp format selector.

    Handles compound selectors ("140/best", "251,140") by taking the first
    concrete numeric id it recognises. Returns None when the selector carries
    no id we know, in which case the caller must warn rather than guess.
    """
    for token in re.findall(r'\b(\d{2,4})\b', fmt or ""):
        container = FORMAT_ID_CONTAINER.get(token)
        if container:
            return container
    return None


def resolve_download_profile(
    codec: str, quality: str,
    container_override: str | None,
    format_override: str | None,
) -> tuple[str, str, str, str, bool]:
    """
    Returns (yt_dlp_format_code, codec, container, final_ext, skip_artwork).

    skip_artwork reflects ARTWORK_BACKEND: WebM has no supported embed path,
    while Ogg/Opus is handled through mutagen rather than ffmpeg.
    """
    codec   = (codec or "aac").lower()
    quality = (quality or "premium").lower()

    if format_override:
        inferred = infer_container_from_format(format_override)
        if container_override:
            container = container_override.lower()
            if container not in CONTAINER_EXT:
                print_warn(f"Unknown container '{container}', defaulting to m4a.")
                container = "m4a"
            if inferred and inferred != container:
                print_warn(
                    f"--format {format_override} normally delivers "
                    f"'{inferred}' but --container '{container}' was given — "
                    f"trusting --container."
                )
        elif inferred:
            container = inferred
            print_info(
                f"--format {format_override} implies container "
                f"'{container}' (codec/quality ignored)."
            )
        else:
            container = "m4a"
            print_warn(
                f"Could not infer a container from --format {format_override!r}. "
                f"Assuming m4a — pass --container to match the real output, or "
                f"artwork/globbing may not find the downloaded files."
            )
        final_ext = CONTAINER_EXT[container]
        skip_artwork = (ARTWORK_BACKEND.get(final_ext) == "skip")
        return format_override, codec, container, final_ext, skip_artwork

    fmt = CODEC_FORMAT_MAP.get((codec, quality))
    if fmt is None:
        print_warn(
            f"Unknown codec/quality combination '{codec}/{quality}' — "
            f"falling back to aac/premium."
        )
        codec, quality, fmt = "aac", "premium", "141"

    container = (container_override or CODEC_DEFAULT_CONTAINER[codec]).lower()
    if container not in VALID_CONTAINERS_FOR_CODEC.get(codec, {container}):
        print_warn(
            f"Container '{container}' is not valid for codec '{codec}'. "
            f"Using default '{CODEC_DEFAULT_CONTAINER[codec]}' instead."
        )
        container = CODEC_DEFAULT_CONTAINER[codec]

    final_ext    = CONTAINER_EXT[container]
    skip_artwork = (ARTWORK_BACKEND.get(final_ext) == "skip")
    return fmt, codec, container, final_ext, skip_artwork


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class Job:
    url:             str
    directory:       Optional[str]              = None
    track_selection: Optional[set[int]]         = None
    cd_specs:        list[tuple[str, set[int]]]  = field(default_factory=list)
    resolved_dir:    Optional[Path]             = None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> tuple[argparse.Namespace, list[Job]]:
    """
    Two-phase parsing:
      Phase 1 — extract global flags with parse_known_args.
      Phase 2 — split remaining argv at URL boundaries and parse each job.
    """

    # --- Global flags (default=None so we can distinguish "not passed" from
    #     "passed with the same value as the default" when merging config) ---
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--format",  default=None)
    global_parser.add_argument("--codec",   choices=["aac", "opus"], default=None)
    global_parser.add_argument("--quality", choices=["premium", "standard"], default=None)
    global_parser.add_argument("--container", choices=["m4a", "ogg", "webm"], default=None)
    global_parser.add_argument("--browser", default=None)
    global_parser.add_argument("--size",    type=int, default=None)
    global_parser.add_argument("--max-workers", type=int, default=None, dest="max_workers")
    global_parser.add_argument("--retries", type=int, default=None)
    global_parser.add_argument("--dry-run", action="store_true")
    global_parser.add_argument(
        "--per-track-art", action="store_true", default=None, dest="per_track_art",
        help="Crop a distinct square from each track instead of sharing the playlist cover",
    )
    global_parser.add_argument("--log",     metavar="FILE", nargs="?",
                               const="auto", default=None)

    global_ns, remaining = global_parser.parse_known_args()

    # The global parser has add_help=False, so -h/--help lands in `remaining`
    # and used to be handed to the per-job parser, which errored out with the
    # "[job]" usage instead of the real one.
    if any(a in ("-h", "--help") for a in remaining):
        global_parser.print_help()
        print(
            "\nPer-job options:\n"
            "  url                 YouTube Music album/playlist/video URL\n"
            "  --dir PATH          Output directory (relative or absolute)\n"
            "  --ts SPEC           Track selection, e.g. 1-5,8,12-14\n"
            "  --cd FOLDER:SPEC    Multi-disc; repeatable, e.g. --cd CD1:1-17\n"
            "\nExamples:\n"
            '  ytm-dlp URL --dir "Artist/Album"\n'
            '  ytm-dlp URL1 --dir "Artist/Album1", URL2 --dir "Artist/Album2"\n'
            '  ytm-dlp URL --dir "Artist/Album" --cd "CD1:1-17" --cd "CD2:18-30"\n'
        )
        sys.exit(0)

    # --- Split remaining into per-job segments at URL boundaries ---
    segments: list[list[str]] = []
    current:  list[str]       = []
    for arg in remaining:
        if arg.startswith("http") and current:
            segments.append(current)
            current = [arg]
        else:
            current.append(arg)
    if current:
        segments.append(current)

    if not segments:
        global_parser.print_help()
        print("\nError: at least one URL is required.")
        sys.exit(1)

    # --- Per-job parser ---
    job_parser = argparse.ArgumentParser(prog="[job]", add_help=False)
    job_parser.add_argument("url")
    job_parser.add_argument("--dir", dest="directory", default=None)
    job_parser.add_argument("--ts",  default=None)
    job_parser.add_argument("--cd",  action="append", default=None)

    jobs: list[Job] = []
    for seg in segments:
        ns = job_parser.parse_args(seg)

        if ns.ts and ns.cd:
            print(f"Error: --ts and --cd cannot be used together in job: {ns.url}")
            sys.exit(1)

        ts: Optional[set[int]] = None
        if ns.ts:
            try:
                ts = parse_track_selection(ns.ts)
            except ValueError as e:
                print(f"Error in --ts spec '{ns.ts}': {e}")
                sys.exit(1)
            if not ts:
                print(f"Error: --ts spec '{ns.ts}' selected no tracks.")
                sys.exit(1)

        cd_specs: list[tuple[str, set[int]]] = []
        if ns.cd:
            for spec in ns.cd:
                try:
                    cd_specs.append(parse_cd_spec(spec))
                except ValueError as e:
                    print(f"Error in --cd spec '{spec}': {e}")
                    sys.exit(1)

            # Warn (but don't block) if the same track number appears in
            # more than one disc — likely unintended, but may be deliberate.
            if len(cd_specs) > 1:
                seen_tracks: dict[int, str] = {}
                for folder, tracks in cd_specs:
                    for t in sorted(tracks):
                        if t in seen_tracks and seen_tracks[t] != folder:
                            print_warn(
                                f"Track {t} appears in both '{seen_tracks[t]}' and "
                                f"'{folder}' — it will be downloaded into both folders."
                            )
                        else:
                            seen_tracks[t] = folder

        jobs.append(Job(
            url=ns.url,
            directory=ns.directory,
            track_selection=ts,
            cd_specs=cd_specs,
        ))

    return global_ns, jobs


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

_WIN_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_folder_name(name: str) -> str:
    sanitized = _WIN_ILLEGAL_RE.sub('', name).strip()
    sanitized = sanitized.rstrip('. ')
    return sanitized


def ask(prompt: str) -> str | None:
    """
    Prompt the user, returning None when there is no interactive terminal
    (piped input, CI, a scheduler). Callers treat None as "cancel" instead of
    letting EOFError escape as a traceback.
    """
    try:
        return input(prompt).strip().upper()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def resolve_directory(dir_spec: str, dry_run: bool) -> Path | None:
    spec = Path(dir_spec.replace("\\", os.sep))
    # An absolute spec must not have its anchor sanitised: 'C:\' would become
    # 'C' and the run would try to create ./C/... instead of using the drive.
    if spec.drive or spec.root:
        current = Path(spec.anchor)
        parts   = spec.parts[1:]
    else:
        current = Path.cwd()
        parts   = spec.parts

    for raw_part in parts:
        # '.' and '..' are legitimate path segments, not illegal characters.
        # Letting rstrip('. ') through used to silently drop them, which
        # resolved --dir "..\Album" to the current directory instead.
        part = raw_part if raw_part in (os.curdir, os.pardir) \
            else sanitize_folder_name(raw_part)
        if part != raw_part:
            print_warn(
                f"Folder name contained illegal characters and was adjusted: "
                f"'{raw_part}' → '{part}'"
            )
        if not part:
            print_warn(f"Folder name '{raw_part}' reduced to empty after sanitizing — skipping.")
            continue

        target = current / part
        if target.exists():
            if not target.is_dir():
                print_warn(f"'{target}' exists but is not a directory. Cancelling.")
                return None
            current = target
            continue

        print_warn(f"Directory does not exist: {target}")
        while True:
            raw = ask(
                f"  Create {Fore.CYAN}'{part}'{Style.RESET_ALL}? "
                f"[{Fore.GREEN}Y{Style.RESET_ALL}es / "
                f"{Fore.RED}N{Style.RESET_ALL}o / "
                f"{Fore.YELLOW}M{Style.RESET_ALL}odify name] "
            )
            if raw is None:
                print_warn("No interactive terminal available — cannot create directories.")
                return None

            if raw == "Y":
                if not dry_run:
                    target.mkdir(parents=False, exist_ok=True)
                    print_success(f"  Created: {target}")
                else:
                    print_dry(f"Would create: {target}")
                current = target
                break

            elif raw == "N":
                print_info("Operation cancelled.")
                return None

            elif raw == "M":
                try:
                    new_name = input(f"  Enter new name for '{part}': ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    print_warn("No interactive terminal available — cannot rename.")
                    return None
                if not new_name:
                    print_warn("  No name entered — try again.")
                    continue
                new_name = sanitize_folder_name(new_name)
                if not new_name:
                    print_warn("  Name still invalid after sanitizing — try again.")
                    continue
                target = current / new_name
                if target.exists() and not target.is_dir():
                    print_warn(f"  '{target}' already exists as a file. Try again.")
                    continue
                if not dry_run:
                    target.mkdir(parents=False, exist_ok=True)
                    print_success(f"  Created: {target}")
                else:
                    print_dry(f"Would create: {target}")
                current = target
                break

            else:
                print_warn("  Please enter Y, N, or M.")

    return current


def resolve_all_directories(jobs: list[Job], dry_run: bool) -> bool:
    needs_resolution = [j for j in jobs if j.directory]
    if not needs_resolution:
        return True

    if len(needs_resolution) > 1:
        print_info(f"Checking {len(needs_resolution)} target director(ies) upfront...")

    for job in needs_resolution:
        resolved = resolve_directory(job.directory, dry_run)
        if resolved is None:
            return False
        job.resolved_dir = resolved

    return True


def parse_cd_spec(spec: str) -> tuple[str, set[int]]:
    if ":" not in spec:
        raise ValueError("Expected format 'FolderName:track-range', e.g. 'CD1:1-17'")
    idx    = spec.rfind(":")
    folder = spec[:idx].strip()
    tracks = parse_track_selection(spec[idx + 1:].strip())
    if not folder:
        raise ValueError("Missing folder name.")
    if not tracks:
        raise ValueError("Empty track range.")
    return folder, tracks


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def parse_track_selection(spec: str) -> set[int]:
    """
    Parse "1-5,8,12-14" into a set of track numbers.

    Raises ValueError with a readable message rather than letting int() raise
    a bare "invalid literal for int()" traceback.
    """
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        bounds = part.split("-")
        if len(bounds) > 2:
            raise ValueError(f"invalid range '{part}' (expected 'lo-hi')")
        try:
            if len(bounds) == 2:
                lo, hi = int(bounds[0].strip()), int(bounds[1].strip())
                if hi < lo:
                    raise ValueError(f"range '{part}' ends before it starts")
                result.update(range(lo, hi + 1))
            else:
                result.add(int(part))
        except ValueError as e:
            raise ValueError(f"'{part}' is not a valid track number or range") from e
    return result


def is_album_url(url: str) -> bool:
    return bool(re.search(r'browse/MPRE|playlist\?list=OLAK5uy_', url))


def square_thumbnail_candidates(thumbnails: list, size: int = 600) -> list[str]:
    """
    Every square thumbnail URL, largest first.

    YouTube regularly advertises a resolution that does not exist — an album's
    1200x1200 "maxresdefault" entry carries no signature and 404s — so callers
    must try these in order instead of trusting the largest one.
    """
    square = [
        t for t in (thumbnails or [])
        if t.get("url") and t.get("width") and t.get("height")
        and t["width"] == t["height"]
    ]
    square.sort(key=lambda t: t["width"], reverse=True)
    return [re.sub(r'w\d+-h\d+', f'w{size}-h{size}', t["url"]) for t in square]


def best_square_thumbnail(thumbnails: list, size: int = 600) -> str | None:
    candidates = square_thumbnail_candidates(thumbnails, size)
    return candidates[0] if candidates else None


def widest_thumbnail_url(thumbnails: list) -> str | None:
    """
    Largest thumbnail of any aspect ratio.

    Individual YouTube tracks expose no square artwork at all (yt-dlp has no
    musicVideoThumbnailRenderer handling), so this is what per-track artwork
    gets centre-cropped from.
    """
    usable = [t for t in (thumbnails or []) if t.get("url") and t.get("width")]
    if not usable:
        return None
    return max(usable, key=lambda t: t["width"])["url"]


def sniff_image_mime(path: Path) -> str | None:
    """
    Identify a downloaded file by its magic bytes.

    Servers happily return an HTML error/captcha page with HTTP 200, and a
    connection dropped mid-transfer leaves a truncated file behind. Either one
    used to reach ffmpeg, which then failed *after* creating its output file —
    and the old code replaced the audio with that broken output. Rejecting
    non-images here removes the trigger entirely.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if head.startswith(b'\xff\xd8\xff'):
        return "image/jpeg"
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return "image/png"
    if head.startswith((b'GIF87a', b'GIF89a')):
        return "image/gif"
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return "image/webp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int]:
    """
    Minimal JPEG SOF / PNG IHDR dimension probe so METADATA_BLOCK_PICTURE
    carries real pixel dimensions. Avoids a Pillow dependency just to read
    four integers.
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        width  = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    if not data.startswith(b'\xff\xd8'):
        return 0, 0
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in sof:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width  = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        i += 2 + seg_len
    return 0, 0


def download_image(url: str, dest: Path, timeout: int = 20) -> bool:
    """
    Download a thumbnail, validating it is a real, complete image.

    Uses urlopen with an explicit timeout rather than urlretrieve, which offers
    no portable way to bound the transfer and could block indefinitely on a
    stalled connection.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ytm-dlp/1.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                print_warn(f"Image request returned HTTP {response.status}")
                return False
            with open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh, 64 * 1024)
    except Exception as e:
        print_warn(f"Could not download image: {e}")
        dest.unlink(missing_ok=True)          # never leave a partial file behind
        return False

    mime = sniff_image_mime(dest)
    if mime is None:
        print_warn(f"Discarding non-image response from {url}")
        dest.unlink(missing_ok=True)
        return False
    return True


def download_first_available(urls: list[str], dest: Path) -> str | None:
    """
    Try each URL in order and return the first that yields a usable image.

    Confirms the file really landed, not just that the fetch reported success.
    """
    for url in urls:
        if download_image(url, dest) and dest.exists() and dest.stat().st_size > 0:
            return url
        dest.unlink(missing_ok=True)
    return None


def crop_to_square(src: Path, dest: Path, size: int) -> bool:
    """
    Centre-crop a thumbnail to a square and scale it, overwriting dest.

    This is how per-track artwork is produced: YouTube exposes no square
    thumbnail for an individual track, so its largest available frame is
    cropped instead. Uses the same verify-before-replace discipline as
    mux_artwork so a failed crop can never clobber a good file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=dest.suffix)
    os.close(fd)
    temp = Path(tmp_name)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}",
             "-frames:v", "1", "-q:v", "2",
             str(temp), "-hide_banner", "-loglevel", "error"],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            print_warn(f"Could not crop artwork: "
                       f"{detail[-1] if detail else f'exit {proc.returncode}'}")
            return False
        if not temp.exists() or temp.stat().st_size == 0:
            return False
        if sniff_image_mime(temp) is None:
            print_warn("Crop produced an unreadable image — keeping original.")
            return False
        temp.replace(dest)
        return True
    except Exception as e:
        print_warn(f"ffmpeg exception while cropping artwork: {e}")
        return False
    finally:
        temp.unlink(missing_ok=True)


def probe_file_metadata(path: Path) -> dict:
    """
    Single ffprobe call returning everything needed for artwork matching:
    the embedded title, comment/purl (source URL) tags, and whether an
    artwork stream is already attached.

    Tag location differs by container, which is why this reads both levels:
      * MP4/M4A exposes them under format.tags
      * Ogg/Opus exposes Vorbis comments under the *audio* stream's tags
        (format.tags is empty there)
      * an attached picture shows up as a video stream for both

    Only audio-stream tags are merged: an attached picture carries its own
    `comment` ("Cover (front)") that would otherwise shadow the real URL.
    """
    default = {"title": None, "comment": None, "purl": None, "has_artwork": False}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             "format_tags=title,comment,purl:"
             "stream_tags=title,comment,purl:"
             "stream=codec_type",
             "-of", "json",
             str(path)],
            capture_output=True, text=True
        )
        data    = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []

        tags: dict[str, str] = {}
        for key, val in ((data.get("format") or {}).get("tags") or {}).items():
            tags[str(key).lower()] = val
        for s in streams:
            if s.get("codec_type") != "audio":
                continue
            for key, val in (s.get("tags") or {}).items():
                tags.setdefault(str(key).lower(), val)

        return {
            "title":       tags.get("title"),
            "comment":     tags.get("comment"),
            "purl":        tags.get("purl"),
            "has_artwork": any(s.get("codec_type") == "video" for s in streams),
        }
    except Exception:
        return default


def _verify_muxed_output(path: Path, original: Path) -> bool:
    """
    Confirm the muxed file is playable before it replaces the original.
    Guards against ffmpeg exiting non-zero yet still leaving an output file
    behind (it does exactly that when the input image is undecodable).
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        orig_size = original.stat().st_size
        # A successful tag/stream copy must not shrink the audio noticeably.
        if orig_size and path.stat().st_size < orig_size * 0.9:
            return False
    except OSError:
        return False
    return probe_file_metadata(path)["has_artwork"]


def mux_artwork(audio: Path, image: Path, ext: str) -> bool:
    """Attach artwork to an MP4/M4A file using an attached_pic video stream."""
    # NOTE: tempfile.NamedTemporaryFile(delete=True) cannot be used here — on
    # Windows it opens the file with an exclusive delete-on-close handle, so
    # ffmpeg cannot open it for writing. mkstemp + close gives us a unique,
    # unlocked, same-directory path instead.
    fd, tmp_name = tempfile.mkstemp(dir=str(audio.parent), suffix=f".{ext}")
    os.close(fd)
    temp = Path(tmp_name)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(audio), "-i", str(image),
             "-map", "0", "-map", "1",
             "-c", "copy", "-disposition:v:0", "attached_pic",
             str(temp), "-hide_banner", "-loglevel", "error"],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            print_warn(
                f"ffmpeg refused to attach artwork: "
                f"{detail[-1] if detail else f'exit {proc.returncode}'}"
            )
            return False
        if not _verify_muxed_output(temp, audio):
            print_warn("ffmpeg produced an unusable output — keeping original file.")
            return False
        temp.replace(audio)
        return True
    except Exception as e:
        print_warn(f"ffmpeg exception: {e}")
        return False
    finally:
        temp.unlink(missing_ok=True)


def embed_artwork_ogg(audio: Path, image: Path) -> bool:
    """
    Attach artwork to an Ogg/Opus file.

    The Ogg muxer cannot carry an mjpeg stream — ffmpeg aborts with
    "Unsupported codec id in stream 1". The interoperable way to carry a
    picture in Ogg is a base64 METADATA_BLOCK_PICTURE Vorbis comment, which
    mutagen writes in place. ffprobe still reports that picture as a video
    stream, so has_artwork detection keeps working unchanged.
    """
    try:
        data = image.read_bytes()
        pic  = Picture()
        pic.type = 3                                   # front cover
        pic.mime = sniff_image_mime(image) or "image/jpeg"
        pic.data = data
        width, height = image_dimensions(data)
        pic.width, pic.height = width, height
        pic.depth = 24

        audio_file = OggOpus(str(audio))
        audio_file["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
        audio_file.save()
    except Exception as e:
        print_warn(f"Could not embed artwork: {e}")
        return False

    # Re-read to confirm the picture actually landed.
    try:
        pics = OggOpus(str(audio)).get("METADATA_BLOCK_PICTURE") or []
        if not pics:
            print_warn("Artwork tag did not persist — keeping original file.")
            return False
    except Exception as e:
        print_warn(f"Could not verify embedded artwork: {e}")
        return False
    return True


def embed_artwork(audio: Path, image: Path, backend: str, ext: str) -> bool:
    # Validate here too, not just at download time: mutagen will happily store
    # arbitrary bytes as a "picture", so an unvalidated file would embed an
    # HTML error page as cover art and still report success.
    if sniff_image_mime(image) is None:
        print_warn(f"{image.name} is not a readable image — skipping artwork.")
        return False
    # `-map 0 -map 1` against a file that already carries an attached picture
    # would add a *second* video stream, so never embed twice.
    if probe_file_metadata(audio)["has_artwork"]:
        return True
    if backend == "ffmpeg":
        return mux_artwork(audio, image, ext)
    if backend == "mutagen":
        return embed_artwork_ogg(audio, image)
    return False


def cleanup_yt_dlp_temps(directory: Path, ext: str):
    for f in directory.glob(f"*.temp.{ext}"):
        try:
            f.unlink()
            print_temp(f"Removed leftover temp file: {f.name}")
        except OSError as e:
            print_warn(f"Could not remove {f.name}: {e}")


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------

def build_ydl_opts(fmt: str, codec: str, container: str, browser: str,
                   retries: int, dry_run: bool,
                   track_selection: set[int] | None = None,
                   output_dir: Path | None = None) -> dict:
    # "03 - Title.m4a" for playlist entries, plain "Title.m4a" for a bare
    # video URL (playlist_index is absent there, which used to yield the
    # literal "NA Title.m4a"). Zero-padding also makes name sorting numeric.
    outtmpl = "%(playlist_index&{:02d} - |)s%(title)s.%(ext)s"
    if output_dir:
        outtmpl = str(output_dir / outtmpl)

    postprocessors = []

    if codec == "opus" and container == "ogg":
        # Source is already Opus (webm delivery) — this is a fast remux to
        # Ogg-Opus (.opus), not a re-encode.
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "0",
        })

    postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})

    opts = {
        "format": fmt,
        "cookiesfrombrowser": (browser,),
        "addmetadata": True,
        "postprocessors": postprocessors,
        "outtmpl":          outtmpl,
        "retries":          retries,
        "fragment_retries": retries,
        "simulate":         dry_run,
        "quiet":            False,
        "no_warnings":      False,
        "ignoreerrors":     True,
    }
    if track_selection:
        opts["playlist_items"] = ",".join(str(i) for i in sorted(track_selection))
    return opts


def fetch_thumbnails_for_track(url: str, browser: str, playlist_item: str = "1") -> list:
    opts = {
        "cookiesfrombrowser": (browser,),
        "quiet": True, "no_warnings": True,
        "playlist_items": playlist_item,
        "skip_download": True, "simulate": True, "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                for e in (info.get("entries") or [info]):
                    if e:
                        return e.get("thumbnails", [])
    except Exception as e:
        print_warn(f"Could not fetch thumbnails: {e}")
    return []


def fetch_playlist_metadata(url: str, browser: str) -> tuple[list[dict], list]:
    """
    Return (entries, playlist_thumbnails).

    playlist_thumbnails matters: YouTube Music puts the square artwork on the
    playlist/album itself (up to 1200x1200) and gives individual tracks none at
    all, so it is the only source of square art for a YT Music playlist.
    """
    entries: list[dict] = []
    playlist_thumbnails: list = []
    opts = {
        "cookiesfrombrowser": (browser,),
        "quiet": True, "no_warnings": True,
        "skip_download": True, "simulate": True, "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return entries, playlist_thumbnails
            playlist_thumbnails = info.get("thumbnails") or []
            raw   = info.get("entries") or [info]
            total = len(raw)
            for i, e in enumerate(raw, start=1):
                if not e:
                    continue
                entries.append({
                    "index":       str(e.get("playlist_index") or "0"),
                    "title":       e.get("title", "Unknown"),
                    "webpage_url": e.get("webpage_url", ""),
                    "thumbnails":  e.get("thumbnails", []),
                })
                print(
                    f"\r  {Fore.CYAN}Loaded {i}/{total} track(s)...{Style.RESET_ALL}",
                    end="", flush=True
                )
            print()
    except Exception as ex:
        print()
        print_warn(f"Metadata fetch error: {ex}")
    return entries, playlist_thumbnails


# ---------------------------------------------------------------------------
# Artwork pre-passes
# ---------------------------------------------------------------------------

def fetch_album_artwork(url: str, size: int, dry_run: bool,
                        output_dir: Path, browser: str) -> Path | None:
    print_info("Album detected — fetching single square artwork...")
    if dry_run:
        print_dry("Would fetch album artwork.")
        return None

    # Prefer the album's own artwork; fall back to the first track, whose
    # thumbnail is not always the album cover.
    candidates: list[str] = []
    try:
        with yt_dlp.YoutubeDL({
            "cookiesfrombrowser": (browser,),
            "quiet": True, "no_warnings": True,
            "skip_download": True, "simulate": True, "ignoreerrors": True,
            "playlist_items": "1",
        }) as ydl:
            info = ydl.extract_info(url, download=False)
        if info:
            candidates = square_thumbnail_candidates(info.get("thumbnails") or [], size)
            if not candidates:
                candidates = square_thumbnail_candidates(
                    fetch_thumbnails_for_track(url, browser, playlist_item="1"), size
                )
    except Exception as e:
        print_warn(f"Could not fetch album artwork: {e}")

    if not candidates:
        print_warn("No square thumbnail found, artwork will be skipped.")
        return None

    # Deliberately NOT "cover.jpg": that is a conventional file in an album
    # folder, and this script overwrote and then deleted it. Use a hidden,
    # content-addressed name so a user's own cover.jpg is never touched.
    for url in candidates:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        cover = output_dir / f".ytm-cover_{digest}.jpg"
        if download_image(url, cover):
            print_success("Square artwork saved.")
            return cover
    print_warn("Could not download album artwork — it will be skipped.")
    return None


def fetch_playlist_artwork(
    url: str, size: int, dry_run: bool,
    track_selection: set[int] | None,
    output_dir: Path,
    browser: str,
    max_workers: int = 8,
    per_track: bool = False,
) -> tuple[dict[str, Path], dict[str, Path], set[Path]]:
    """
    Downloads artwork for each track in the playlist, in parallel.

    Artwork is resolved per track in tiers, because YouTube Music exposes no
    square thumbnail on individual tracks at all:

      1. the track's own square thumbnail, when the source provides one
      2. the playlist/album's square artwork (the normal YT Music path)
      3. with per_track=True, the track's largest frame centre-cropped to a
         square — distinct per track, but a video still rather than real cover
         art; used for mixed playlists where one shared cover is unhelpful

    Returns:
      by_url:         webpage_url -> thumbnail Path  (primary lookup key)
      by_title:       title       -> thumbnail Path  (fallback lookup key)
      all_thumbnails: every downloaded thumbnail Path (used for cleanup)
    """
    print_info("Playlist detected — fetching square artwork...")
    by_url:   dict[str, Path] = {}
    by_title: dict[str, Path] = {}
    all_thumbnails: set[Path] = set()

    print_info("Retrieving playlist metadata...")
    entries, playlist_thumbnails = fetch_playlist_metadata(url, browser)
    if not entries:
        print_warn("Could not retrieve playlist metadata.")
        return by_url, by_title, all_thumbnails

    if track_selection:
        entries = [e for e in entries if int(e["index"]) in track_selection]

    print_info(f"Found {len(entries)} track(s). Fetching artwork...")

    own_square = sum(1 for e in entries
                     if square_thumbnail_candidates(e["thumbnails"], size))
    if not own_square:
        print_info(
            "No track exposes its own square artwork — "
            + ("cropping each track's largest frame instead."
               if per_track else "using the playlist cover for every track.")
        )

    if dry_run:
        cover_candidates = square_thumbnail_candidates(playlist_thumbnails, size)
        for entry in entries:
            display = f"{entry['index']} {entry['title']}"
            if square_thumbnail_candidates(entry["thumbnails"], size):
                print_dry(f"Would fetch artwork for: {display}")
            elif cover_candidates:
                print_dry(f"Would use the playlist cover for: {display}")
            elif per_track and widest_thumbnail_url(entry["thumbnails"]):
                print_dry(f"Would crop a square frame for: {display}")
            else:
                print_warn(f"No square artwork available for: {display}")
        return by_url, by_title, all_thumbnails

    # Tier 2: the playlist's own cover, shared by every track. Hidden and
    # content-addressed so a user's cover.jpg is never touched. Candidates are
    # tried largest-first because YouTube advertises album sizes that 404.
    # Fetched lazily: in per-track mode it is only a last resort, so eagerly
    # downloading it would cost a request and leave an unused file behind.
    cover_path: Path | None = None
    cover_lock = threading.Lock()
    cover_candidates = square_thumbnail_candidates(playlist_thumbnails, size)

    def get_cover() -> Path | None:
        nonlocal cover_path
        if cover_path is not None or not cover_candidates:
            return cover_path
        with cover_lock:
            if cover_path is not None:
                return cover_path
            for url in cover_candidates:
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
                candidate = output_dir / f".ytm-playlist-cover_{digest}.jpg"
                if download_image(url, candidate):
                    cover_path = candidate
                    all_thumbnails.add(candidate)
                    break
        return cover_path

    if not per_track:
        get_cover()

    img_url_cache: dict[str, Path] = {}
    url_locks: dict[str, threading.Lock] = {}
    url_locks_guard = threading.Lock()

    def get_url_lock(img_url: str) -> threading.Lock:
        with url_locks_guard:
            lock = url_locks.get(img_url)
            if lock is None:
                lock = threading.Lock()
                url_locks[img_url] = lock
            return lock

    def process_entry(entry: dict):
        track_url = entry["webpage_url"]
        title     = entry["title"]
        display   = f"{entry['index']} {title}"

        # Tier 1 — the track's own square artwork.
        own = square_thumbnail_candidates(entry["thumbnails"], size)
        if own:
            lock = get_url_lock(own[0])
            with lock:
                cached = img_url_cache.get(own[0])
                if cached is not None:
                    print_art_reuse(display)
                    return title, track_url, cached
                digest = hashlib.sha256(own[0].encode("utf-8")).hexdigest()[:10]
                safe = re.sub(r'[^\w\s-]', '', title)[:32].strip()
                thumb_path = output_dir / f"thumb_{digest}_{safe}.jpg"
                if download_first_available(own, thumb_path):
                    img_url_cache[own[0]] = thumb_path
                    print_art(display)
                    return title, track_url, thumb_path
            print_warn(f"Failed to download artwork for: {display}")

        # Tier 2 — the shared playlist/album cover.
        # Tier 3 (opt-in) — a distinct square cropped from this track's own
        # frame. Tried before the shared cover because that is the whole point
        # of per_track: one cover for a mixed playlist identifies nothing.
        if per_track:
            frame = widest_thumbnail_url(entry["thumbnails"])
            if frame:
                digest = hashlib.sha256(frame.encode("utf-8")).hexdigest()[:10]
                safe = re.sub(r'[^\w\s-]', '', title)[:32].strip()
                thumb_path = output_dir / f"thumb_{digest}_{safe}.jpg"
                if download_image(frame, thumb_path) and crop_to_square(
                        thumb_path, thumb_path, size):
                    print_art(f"{display} (cropped)")
                    return title, track_url, thumb_path
                thumb_path.unlink(missing_ok=True)

        if cover_path is not None or get_cover() is not None:
            print_art_reuse(f"{display} (playlist cover)")
            return title, track_url, cover_path

        print_warn(f"No square artwork available for: {display}")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_entry, entry) for entry in entries]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is None:
                continue
            title, track_url, thumb_path = result
            all_thumbnails.add(thumb_path)
            if track_url:
                by_url[track_url] = thumb_path
            by_title[title] = thumb_path

    unique = len(img_url_cache) + (1 if cover_path is not None else 0)
    summary = f"Fetched {unique} unique artwork image(s)"
    if cover_path is not None and not img_url_cache:
        summary += " (playlist cover shared by all tracks)"
    print_success(summary + ".")
    return by_url, by_title, all_thumbnails


# ---------------------------------------------------------------------------
# Core download + mux pass
# ---------------------------------------------------------------------------

def run_download_pass(
    url:             str,
    fmt:             str,
    codec:           str,
    container:       str,
    final_ext:       str,
    retries:         int,
    dry_run:         bool,
    album:           bool,
    size:            int,
    max_workers:     int,
    track_selection: set[int] | None,
    output_dir:      Path,
    browser:         str,
    logger:          logging.Logger,
    label:           str = "",
    per_track:       bool = False,
):
    prefix = f"{label} " if label else ""

    cover_path:     Path | None     = None
    by_url:         dict[str, Path] = {}
    by_title:       dict[str, Path] = {}
    all_thumbnails: set[Path]       = set()

    backend = ARTWORK_BACKEND.get(final_ext, "skip")
    if backend == "mutagen" and not HAVE_MUTAGEN:
        backend = "skip"
        print_warn(
            f"{prefix}mutagen is not installed (pip install mutagen) — "
            f"skipping artwork for {container} files."
        )
    skip_artwork = (backend == "skip")

    if skip_artwork:
        print_warn(
            f"{prefix}Container '{container}' does not support embedded "
            f"artwork — skipping artwork step."
        )
    elif album:
        cover_path = fetch_album_artwork(url, size, dry_run, output_dir, browser)
    else:
        by_url, by_title, all_thumbnails = fetch_playlist_artwork(
            url, size, dry_run, track_selection, output_dir, browser, max_workers,
            per_track=per_track,
        )

    # Record the scratch files now that they exist, so a Ctrl+C anywhere below
    # (download, probing, muxing) still removes them from the music folder.
    _register_artwork(album, cover_path, all_thumbnails)

    existing_files = {
        f.resolve() for f in output_dir.glob(f"*.{final_ext}")
        if not f.name.endswith(f".temp.{final_ext}")
    }

    print_info(f"{prefix}Initiating audio stream download...")
    if dry_run:
        print_dry(f"{prefix}Would run yt-dlp download.")
    else:
        ydl_opts = build_ydl_opts(fmt, codec, container, browser, retries, dry_run=False,
                                  track_selection=track_selection,
                                  output_dir=output_dir)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            print_warn(f"yt-dlp reported errors during download: {e}")

    if dry_run:
        print_dry(f"{prefix}Dry run complete — no files downloaded or modified.")
        _cleanup(album, cover_path, all_thumbnails)
        return

    cleanup_yt_dlp_temps(output_dir, final_ext)

    # Safety net for a mis-predicted extension (e.g. an exotic --format):
    # adopt whatever actually landed rather than reporting an empty result.
    if not any(output_dir.glob(f"*.{final_ext}")):
        detected = next(
            (ext for ext in AUDIO_EXTS if any(output_dir.glob(f"*.{ext}"))), None
        )
        if detected and detected != final_ext:
            print_warn(
                f"{prefix}Expected '.{final_ext}' but found '.{detected}' — "
                f"using '.{detected}' for this run."
            )
            final_ext  = detected
            container  = {v: k for k, v in CONTAINER_EXT.items()}.get(detected, container)
            backend    = ARTWORK_BACKEND.get(detected, "skip")
            if backend == "mutagen" and not HAVE_MUTAGEN:
                backend = "skip"
            skip_artwork = (backend == "skip")
            cleanup_yt_dlp_temps(output_dir, final_ext)

    if skip_artwork:
        count = len(list(output_dir.glob(f"*.{final_ext}")))
        msg = f"{prefix}Done. {count} file(s) downloaded (artwork skipped for {container})."
        print_info(msg)
        logger.info(msg)
        _cleanup(album, cover_path, all_thumbnails)
        return

    all_files = sorted(
        [f for f in output_dir.glob(f"*.{final_ext}") if not f.name.endswith(f".temp.{final_ext}")],
        key=lambda f: f.name
    )
    new_files = [f for f in all_files if f.resolve() not in existing_files]

    print_info(f"{prefix}Checking pre-existing files for missing artwork...")
    probe_cache: dict[Path, dict] = {}
    files_lacking_art: list[Path] = []
    for f in all_files:
        if f.resolve() in existing_files:
            meta = probe_file_metadata(f)
            probe_cache[f] = meta
            if not meta["has_artwork"]:
                files_lacking_art.append(f)

    files_to_mux = sorted(
        set(new_files) | set(files_lacking_art), key=lambda f: f.name
    )

    if not files_to_mux:
        print_success(f"{prefix}All files already have artwork — nothing to mux.")
        _cleanup(album, cover_path, all_thumbnails)
        return

    print_info(f"{prefix}Muxing artwork into {len(files_to_mux)} file(s)...")
    muxed   = 0
    skipped = 0

    for audio_file in files_to_mux:
        if album:
            thumb = cover_path
        else:
            meta = probe_cache.get(audio_file) or probe_file_metadata(audio_file)

            src_url = meta.get("comment") or meta.get("purl")
            thumb   = by_url.get(src_url) if src_url else None

            if thumb is None:
                file_title = meta.get("title")
                if file_title:
                    thumb = by_title.get(file_title)
                    if thumb is None:
                        lower = file_title.lower()
                        thumb = next(
                            (v for k, v in by_title.items()
                             if k.lower() == lower),
                            None
                        )

        if not thumb or not thumb.exists():
            print_skip(audio_file.name)
            logger.warning("%sSKIP  %s", prefix, audio_file.name)
            skipped += 1
            continue

        if embed_artwork(audio_file, thumb, backend, final_ext):
            print_ok(audio_file.name)
            logger.info("%sOK    %s", prefix, audio_file.name)
            muxed += 1
        else:
            print_fail(f"Artwork embed failed: {audio_file.name}")
            logger.error("%sFAIL  %s", prefix, audio_file.name)
            skipped += 1

    _cleanup(album, cover_path, all_thumbnails)

    summary = f"{prefix}Done. Muxed: {muxed} | Skipped/Failed: {skipped}"
    print_info(summary)
    logger.info(summary)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "--init-config" in sys.argv[1:]:
        write_default_config(DEFAULT_CONFIG_PATH)
        sys.exit(0)

    global_ns, jobs = parse_args()

    config = load_config(DEFAULT_CONFIG_PATH)
    if not DEFAULT_CONFIG_PATH.exists():
        print_info(
            f"Tip: no config file found — using built-in defaults. "
            f"Run 'ytm-dlp --init-config' to create one at {DEFAULT_CONFIG_PATH}."
        )

    def resolved(cli_val, section, key, hardcoded_default):
        if cli_val is not None:
            return cli_val
        return cfg_get(config, section, key, hardcoded_default)

    browser         = resolved(global_ns.browser,     "download", "browser",         "firefox")
    codec_setting   = resolved(global_ns.codec,       "download", "codec",           "aac")
    quality         = resolved(global_ns.quality,     "download", "quality",         "premium")
    container_o     = resolved(global_ns.container,   "download", "container",       "") or None
    format_override = resolved(global_ns.format,      "download", "format_override", "") or None
    retries         = resolved(global_ns.retries,     "download", "retries",         10)
    size            = resolved(global_ns.size,        "artwork",  "size",            600)
    max_workers     = resolved(global_ns.max_workers, "artwork",  "max_workers",     8)
    log_setting     = resolved(global_ns.log,         "behavior", "log",             "off")

    # None-able store_true so the config file can supply the default.
    if global_ns.per_track_art is not None:
        per_track = global_ns.per_track_art
    else:
        per_track = bool(cfg_get(config, "artwork", "per_track", False))

    if browser.lower() not in KNOWN_BROWSERS:
        print_warn(f"'{browser}' is not a browser ytm-dlp recognizes — passing it through to yt-dlp as-is.")

    # Config values arrive unvalidated, and a bad value used to surface as an
    # unhandled traceback (max_workers=0, or a non-numeric size from the TOML).
    def positive_int(value, name, default, minimum=1):
        try:
            number = int(value)
        except (TypeError, ValueError):
            print_warn(f"Invalid {name}={value!r} in config — using {default}.")
            return default
        if number < minimum:
            print_warn(f"{name} must be >= {minimum} (got {number}) — using {default}.")
            return default
        return number

    retries     = positive_int(retries,     "retries",     10, 0)
    size        = positive_int(size,        "size",        600, 64)
    max_workers = positive_int(max_workers, "max_workers", 8)

    fmt, codec, container, final_ext, skip_artwork = resolve_download_profile(
        codec_setting, quality, container_o, format_override
    )
    print_info(f"Codec/container: {codec}/{container} (.{final_ext})" +
              ("  — artwork embedding will be skipped" if skip_artwork else ""))

    log_path: Path | None = None
    log_value = str(log_setting).strip().lower() if log_setting else "off"
    if log_value not in ("off", ""):
        log_path = Path(
            f"ytm-dlp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            if log_value == "auto" else str(log_setting)
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(log_path)

    if not resolve_all_directories(jobs, global_ns.dry_run):
        sys.exit(0)

    total_jobs = len(jobs)
    try:
        for job_idx, job in enumerate(jobs, start=1):
            album    = is_album_url(job.url)
            base_dir = job.resolved_dir or Path.cwd()

            if total_jobs > 1:
                print_header(f"Job {job_idx}/{total_jobs}  —  {job.url}")

            if job.cd_specs:
                print_info(f"Multi-disc mode: {len(job.cd_specs)} disc(s).")
                for folder_name, disc_tracks in job.cd_specs:
                    sanitized_name = sanitize_folder_name(folder_name)
                    if sanitized_name != folder_name:
                        print_warn(
                            f"Disc folder name contained illegal characters and "
                            f"was adjusted: '{folder_name}' → '{sanitized_name}'"
                        )
                    folder_name = sanitized_name or folder_name

                    print_header(f"Disc: {folder_name}  |  Tracks: {sorted(disc_tracks)}")
                    disc_dir = base_dir / folder_name
                    if not disc_dir.exists():
                        if global_ns.dry_run:
                            print_dry(f"Would create disc folder: {disc_dir}")
                        else:
                            disc_dir.mkdir(parents=False, exist_ok=True)
                            print_success(f"Created disc folder: {disc_dir}")

                    run_download_pass(
                        url=job.url, fmt=fmt, codec=codec, container=container,
                        final_ext=final_ext,
                        retries=retries, dry_run=global_ns.dry_run,
                        album=album, size=size, max_workers=max_workers,
                        track_selection=disc_tracks,
                        output_dir=disc_dir,
                        browser=browser,
                        logger=logger,
                        label=f"[{folder_name}]",
                        per_track=per_track,
                    )

            else:
                if job.track_selection:
                    print_info(f"Track selection: {sorted(job.track_selection)}")

                run_download_pass(
                    url=job.url, fmt=fmt, codec=codec, container=container,
                    final_ext=final_ext,
                    retries=retries, dry_run=global_ns.dry_run,
                    album=album, size=size, max_workers=max_workers,
                    track_selection=job.track_selection,
                    output_dir=base_dir,
                    browser=browser,
                    logger=logger,
                    per_track=per_track,
                )
    except KeyboardInterrupt:
        # 130 is the conventional exit code for SIGINT, and the scratch files
        # this script creates must not be left behind in the music folder.
        print()
        print_warn("Interrupted — cleaning up temporary artwork files.")
        _cleanup_pending_artwork()
        logger.warning("Interrupted by user")
        sys.exit(130)

    if total_jobs > 1:
        print_header(f"All {total_jobs} job(s) complete.")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

# Artwork scratch files for the pass currently in flight, so an interrupt
# anywhere in the job loop can still remove them.
_pending_artwork: list[tuple[bool, Path | None, set[Path]]] = []


def _register_artwork(album: bool, cover_path: Path | None,
                      all_thumbnails: set[Path]) -> None:
    _pending_artwork.append((album, cover_path, all_thumbnails))


def _cleanup_pending_artwork() -> None:
    # Snapshot and empty the registry *before* cleaning: _cleanup() clears it
    # itself, which would otherwise discard the still-pending entries.
    pending, _pending_artwork[:] = list(_pending_artwork), []
    for album, cover_path, all_thumbnails in reversed(pending):
        try:
            _cleanup(album, cover_path, all_thumbnails)
        except OSError:
            pass


def _cleanup(album: bool, cover_path: Path | None, all_thumbnails: set[Path]):
    _pending_artwork.clear()          # this pass is over; nothing left to guard
    if album:
        if cover_path and cover_path.exists():
            cover_path.unlink()
    else:
        for p in all_thumbnails:
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    main()
