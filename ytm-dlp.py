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

Global flags (before first URL, apply to all jobs — all overridable via
config file, see --init-config):
  --format code       Raw yt-dlp format override (advanced; bypasses codec/quality)
  --codec aac|opus     Audio codec (default: aac)
  --quality premium|standard  AAC quality tier (default: premium)
  --container m4a|ogg|webm    Container override (default: per-codec)
  --browser NAME       Browser to pull cookies from (default: firefox)
  --size N             Artwork resolution px (default: 600)
  --max-workers N       Concurrent thumbnail downloads (default: 8)
  --retries N          Download retry count (default: 10)
  --dry-run            Preview without downloading
  --log [FILE]         Write log file
  --init-config        Write a documented config template and exit
"""

import argparse
import concurrent.futures
import json
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Optional

import yt_dlp
from colorama import Fore, Style, init as colorama_init
import time

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
#   opus -> ogg   (.opus — Opus audio in an Ogg container; this is the
#                  standard extension for Ogg-Opus files)
#
# For opus you may explicitly set "webm" instead of "ogg" to keep
# YouTube's native delivery container with no remuxing step.
# NOTE: cover art embedding is NOT supported for webm and will be
# automatically skipped if this is selected.
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


def resolve_download_profile(
    codec: str, quality: str,
    container_override: str | None,
    format_override: str | None,
) -> tuple[str, str, str, str, bool]:
    """
    Returns (yt_dlp_format_code, codec, container, final_ext, skip_artwork).
    """
    codec   = (codec or "aac").lower()
    quality = (quality or "premium").lower()

    if format_override:
        container = (container_override or "m4a").lower()
        if container not in CONTAINER_EXT:
            print_warn(f"Unknown container '{container}', defaulting to m4a.")
            container = "m4a"
        final_ext = CONTAINER_EXT[container]
        skip_artwork = (final_ext == "webm")
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
    skip_artwork = (final_ext == "webm")
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
    global_parser.add_argument("--log",     metavar="FILE", nargs="?",
                               const="auto", default=None)

    global_ns, remaining = global_parser.parse_known_args()

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
            ts = parse_track_selection(ns.ts)

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


def resolve_directory(dir_spec: str, dry_run: bool) -> Path | None:
    parts   = Path(dir_spec.replace("\\", os.sep)).parts
    current = Path.cwd()

    for raw_part in parts:
        part = sanitize_folder_name(raw_part)
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
            raw = input(
                f"  Create {Fore.CYAN}'{part}'{Style.RESET_ALL}? "
                f"[{Fore.GREEN}Y{Style.RESET_ALL}es / "
                f"{Fore.RED}N{Style.RESET_ALL}o / "
                f"{Fore.YELLOW}M{Style.RESET_ALL}odify name] "
            ).strip().upper()

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
                new_name = input(f"  Enter new name for '{part}': ").strip()
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
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo.strip()), int(hi.strip()) + 1))
        else:
            result.add(int(part))
    return result


def is_album_url(url: str) -> bool:
    return bool(re.search(r'browse/MPRE|playlist\?list=OLAK5uy_', url))


def best_square_thumbnail(thumbnails: list, size: int = 600) -> str | None:
    square = [
        t for t in thumbnails
        if t.get("width") and t.get("height") and t["width"] == t["height"]
    ]
    if not square:
        return None
    best = max(square, key=lambda t: t["width"])
    return re.sub(r'w\d+-h\d+', f'w{size}-h{size}', best["url"])


def download_image(url: str, dest: Path) -> bool:
    try:
        urllib.request.urlretrieve(url, dest)
        return dest.exists()
    except Exception as e:
        print_warn(f"Could not download image: {e}")
        return False


def probe_file_metadata(path: Path) -> dict:
    """
    Single ffprobe call returning everything needed for artwork matching:
    the embedded title, comment/purl (source URL) tags, and whether a
    video (artwork) stream is already attached. Works uniformly across
    containers (m4a, opus/ogg).
    """
    default = {"title": None, "comment": None, "purl": None, "has_artwork": False}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format_tags=title,comment,purl:stream=codec_type",
             "-of", "json",
             str(path)],
            capture_output=True, text=True
        )
        data    = json.loads(result.stdout or "{}")
        tags    = (data.get("format") or {}).get("tags") or {}
        streams = data.get("streams") or []
        has_artwork = any(s.get("codec_type") == "video" for s in streams)
        return {
            "title":       tags.get("title"),
            "comment":     tags.get("comment"),
            "purl":        tags.get("purl"),
            "has_artwork": has_artwork,
        }
    except Exception:
        return default


def mux_artwork(audio: Path, image: Path, index: int, ext: str) -> bool:
    temp = Path(f"temp_mux_{index}.{ext}")
    try:
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(audio), "-i", str(image),
             "-map", "0", "-map", "1",
             "-c", "copy", "-disposition:v:0", "attached_pic",
             str(temp), "-hide_banner", "-loglevel", "error"],
            capture_output=True, text=True
        )
        if temp.exists():
            temp.replace(audio)
            return True
        return False
    except Exception as e:
        print_warn(f"ffmpeg exception: {e}")
        return False
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def cleanup_yt_dlp_temps(directory: Path, ext: str):
    for f in directory.glob(f"*.temp.{ext}"):
        f.unlink()
        print_temp(f"Removed leftover temp file: {f.name}")


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------

def build_ydl_opts(fmt: str, codec: str, container: str, browser: str,
                   retries: int, dry_run: bool,
                   track_selection: set[int] | None = None,
                   output_dir: Path | None = None) -> dict:
    outtmpl = "%(playlist_index)s %(title)s.%(ext)s"
    if output_dir:
        outtmpl = str(output_dir / "%(playlist_index)s %(title)s.%(ext)s")

    postprocessors = [
        {
            "key": "MetadataFromField",
            "formats": [
                "%(playlist_index)s:%(track_number)s",
                "%(release_year|upload_date>%Y)s:(?P<meta_date>.*)",
                ":(?P<meta_genre>.*)",
            ],
            "when": "pre_process",
        },
    ]

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


def fetch_playlist_metadata(url: str, browser: str) -> list[dict]:
    entries: list[dict] = []
    opts = {
        "cookiesfrombrowser": (browser,),
        "quiet": True, "no_warnings": True,
        "skip_download": True, "simulate": True, "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return entries
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
    return entries


# ---------------------------------------------------------------------------
# Artwork pre-passes
# ---------------------------------------------------------------------------

def fetch_album_artwork(url: str, size: int, dry_run: bool,
                        output_dir: Path, browser: str) -> Path | None:
    print_info("Album detected — fetching single square artwork...")
    if dry_run:
        print_dry("Would fetch album artwork.")
        return None
    thumbs  = fetch_thumbnails_for_track(url, browser, playlist_item="1")
    img_url = best_square_thumbnail(thumbs, size)
    if not img_url:
        print_warn("No square thumbnail found, artwork will be skipped.")
        return None
    cover = output_dir / "cover.jpg"
    if download_image(img_url, cover):
        print_success("Square artwork saved.")
        return cover
    return None


def fetch_playlist_artwork(
    url: str, size: int, dry_run: bool,
    track_selection: set[int] | None,
    output_dir: Path,
    browser: str,
    max_workers: int = 8,
) -> tuple[dict[str, Path], dict[str, Path], set[Path]]:
    """
    Downloads artwork for each track in the playlist, in parallel.

    Returns:
      by_url:         webpage_url -> thumbnail Path  (primary lookup key)
      by_title:       title       -> thumbnail Path  (fallback lookup key)
      all_thumbnails: every downloaded thumbnail Path (used for cleanup)
    """
    print_info("Playlist detected — fetching per-track square artwork...")
    by_url:   dict[str, Path] = {}
    by_title: dict[str, Path] = {}
    all_thumbnails: set[Path] = set()

    print_info("Retrieving playlist metadata...")
    entries = fetch_playlist_metadata(url, browser)
    if not entries:
        print_warn("Could not retrieve playlist metadata.")
        return by_url, by_title, all_thumbnails

    if track_selection:
        entries = [e for e in entries if int(e["index"]) in track_selection]

    print_info(f"Found {len(entries)} track(s). Fetching artwork...")

    if dry_run:
        for entry in entries:
            display = f"{entry['index']} {entry['title']}"
            img_url = best_square_thumbnail(entry["thumbnails"], size)
            if not img_url:
                print_warn(f"No square thumbnail for: {display}")
            else:
                print_dry(f"Would fetch artwork for: {display}")
        return by_url, by_title, all_thumbnails

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
        img_url   = best_square_thumbnail(entry["thumbnails"], size)

        if not img_url:
            print_warn(f"No square thumbnail for: {display}")
            return None

        lock = get_url_lock(img_url)
        with lock:
            thumb_path = img_url_cache.get(img_url)
            if thumb_path is not None:
                print_art_reuse(display)
                return title, track_url, thumb_path

            safe_title = re.sub(r'[^\w\s-]', '', title)[:60].strip()
            thumb_path = output_dir / f"thumb_{safe_title}.jpg"
            if download_image(img_url, thumb_path):
                img_url_cache[img_url] = thumb_path
                print_art(display)
                return title, track_url, thumb_path
            else:
                print_warn(f"Failed to download artwork for: {display}")
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

    print_success(f"Fetched {len(img_url_cache)} unique artwork image(s).")
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
    skip_artwork:    bool,
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
):
    prefix = f"{label} " if label else ""

    cover_path:     Path | None     = None
    by_url:         dict[str, Path] = {}
    by_title:       dict[str, Path] = {}
    all_thumbnails: set[Path]       = set()

    if skip_artwork:
        print_warn(
            f"{prefix}Container '{container}' does not support embedded "
            f"artwork — skipping artwork step."
        )
    elif album:
        cover_path = fetch_album_artwork(url, size, dry_run, output_dir, browser)
    else:
        by_url, by_title, all_thumbnails = fetch_playlist_artwork(
            url, size, dry_run, track_selection, output_dir, browser, max_workers
        )

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

    if skip_artwork:
        count = len(list(output_dir.glob(f"*.{final_ext}")))
        msg = f"{prefix}Done. {count} file(s) downloaded (artwork skipped for {container})."
        print_info(msg)
        logger.info(msg)
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

    for i, audio_file in enumerate(files_to_mux, start=1):
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

        if mux_artwork(audio_file, thumb, i, final_ext):
            print_ok(audio_file.name)
            logger.info("%sOK    %s", prefix, audio_file.name)
            muxed += 1
        else:
            print_fail(f"ffmpeg error on: {audio_file.name}")
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

    if browser.lower() not in KNOWN_BROWSERS:
        print_warn(f"'{browser}' is not a browser ytm-dlp recognizes — passing it through to yt-dlp as-is.")

    fmt, codec, container, final_ext, skip_artwork = resolve_download_profile(
        codec_setting, quality, container_o, format_override
    )
    print_info(f"Codec/container: {codec}/{container} (.{final_ext})" +
              ("  — artwork embedding will be skipped" if skip_artwork else ""))

    log_path: Path | None = None
    if log_setting and str(log_setting).lower() not in ("off", ""):
        log_path = Path(
            f"ytm-dlp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            if log_setting == "auto" else str(log_setting)
        )

    logger = setup_logging(log_path)

    if not resolve_all_directories(jobs, global_ns.dry_run):
        sys.exit(0)

    total_jobs = len(jobs)
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
                    final_ext=final_ext, skip_artwork=skip_artwork,
                    retries=retries, dry_run=global_ns.dry_run,
                    album=album, size=size, max_workers=max_workers,
                    track_selection=disc_tracks,
                    output_dir=disc_dir,
                    browser=browser,
                    logger=logger,
                    label=f"[{folder_name}]",
                )

        else:
            if job.track_selection:
                print_info(f"Track selection: {sorted(job.track_selection)}")

            run_download_pass(
                url=job.url, fmt=fmt, codec=codec, container=container,
                final_ext=final_ext, skip_artwork=skip_artwork,
                retries=retries, dry_run=global_ns.dry_run,
                album=album, size=size, max_workers=max_workers,
                track_selection=job.track_selection,
                output_dir=base_dir,
                browser=browser,
                logger=logger,
            )

    if total_jobs > 1:
        print_header(f"All {total_jobs} job(s) complete.")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup(album: bool, cover_path: Path | None, all_thumbnails: set[Path]):
    if album:
        if cover_path and cover_path.exists():
            cover_path.unlink()
    else:
        for p in all_thumbnails:
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    main()
