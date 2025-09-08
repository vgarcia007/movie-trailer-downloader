#!/usr/bin/env python3
# manual_trailer_fetch.py
import os
import re
import sys
import argparse
import configparser
import subprocess
import shutil
from typing import Optional, Tuple

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("manual-trailer")

# -------------------- Config --------------------

def load_config(path: str) -> dict:
    """Load trailers.ini and return relevant settings."""
    cfg = configparser.ConfigParser()
    if not os.path.isfile(path):
        log.error(f"Config not found: {path}")
        sys.exit(2)
    cfg.read(path)

    # settings
    lang = cfg.get("settings", "language", fallback="de").strip().lower()
    strict_lang = cfg.getboolean("settings", "strict_language", fallback=False)
    video_exts_raw = cfg.get("settings", "video_exts", fallback="mkv,mp4,m4v,avi,mov")
    video_exts = {"."+e.strip().lower().lstrip(".") for e in video_exts_raw.split(",") if e.strip()}
    trailer_suffix = cfg.get("settings", "trailer_suffix", fallback="-trailer").strip()
    preferred_height = cfg.getint("settings", "preferred_height", fallback=1080)
    temp_dir = cfg.get("settings", "temp_dir", fallback="./tmp").strip()
    allow_non_mp4 = cfg.getboolean("settings", "allow_non_mp4_for_quality", fallback=True)

    temp_dir = os.path.abspath(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)

    return {
        "language": lang,
        "strict_language": strict_lang,
        "video_exts": video_exts,
        "trailer_suffix": trailer_suffix,
        "preferred_height": preferred_height,
        "temp_dir": temp_dir,
        "allow_non_mp4_for_quality": allow_non_mp4,
    }

# -------------------- Helpers --------------------

YOUTUBE_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")

def extract_youtube_id(url_or_id: str) -> Optional[str]:
    """Extract a YouTube video ID from a full URL or return the ID if looks valid."""
    s = url_or_id.strip()
    # raw 11-char id?
    if YOUTUBE_ID_RE.fullmatch(s):
        return s

    # URL variants
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(s)
        if parsed.netloc.endswith("youtube.com"):
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"]:
                vid = qs["v"][0]
                if YOUTUBE_ID_RE.fullmatch(vid):
                    return vid
        if parsed.netloc.endswith("youtu.be"):
            # path like /VIDEOID
            seg = parsed.path.strip("/").split("/")[0]
            if YOUTUBE_ID_RE.fullmatch(seg):
                return seg
    except Exception:
        pass

    # fallback: search first 11-char id pattern anywhere
    m = YOUTUBE_ID_RE.search(s)
    return m.group(1) if m else None


def first_movie_file(path: str, video_exts: set) -> Optional[str]:
    """Pick the largest video file in the folder as the movie."""
    files = [
        f for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f)) and os.path.splitext(f)[1].lower() in video_exts
    ]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getsize(os.path.join(path, f)), reverse=True)
    return files[0]


def build_trailer_target_path(movie_path: str, movie_filename: str, suffix: str) -> str:
    """Return final trailer path (default .mp4; MKV may replace later if needed)."""
    base, _ = os.path.splitext(movie_filename)
    return os.path.join(movie_path, f"{base}{suffix}.mp4")


def get_video_height(path: str) -> Optional[int]:
    """Return height (pixels) for first video stream via ffprobe, or None."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            stderr=subprocess.STDOUT, text=True
        ).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        return None
    return None

# -------------------- yt-dlp download (MP4-first with resilient fallbacks) --------------------

from yt_dlp.utils import DownloadError
from yt_dlp import YoutubeDL
import re as _re

_FRAG_403_RE = _re.compile(r"(HTTP Error 403|fragment\s+1\s+not\s+found)", _re.IGNORECASE)

def _common_ydl_opts(temp_out, temp_dir, format_str, format_sort, preferred_height, label):
    """Baseline yt-dlp options for a given attempt."""
    return {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "outtmpl": temp_out,
        "paths": {"temp": temp_dir},
        "format": format_str,
        "format_sort": format_sort,
        "format_sort_force": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": True,
        "retries": 10,
        "fragment_retries": 10,
        "retry_sleep": {"http": [1, 2, 4, 8], "fragment": [1, 2, 4, 8]},
        "concurrent_fragment_downloads": 1,
        "continuedl": True,
        "ignoreerrors": True,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "postprocessor_args": ["-movflags", "+faststart"],
        "geo_bypass_country": "DE",
        "progress_hooks": [lambda d: (
            d.get("status") == "finished" and
            log.info(f"✓ {label} finished: {d.get('info_dict', {}).get('height')}p "
                     f"{d.get('info_dict', {}).get('ext')} (fmt:{d.get('info_dict', {}).get('format_id')})")
        )],
    }

def _run_download(url, ydl_opts):
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def download_youtube_to_manual(target_path: str, video_id: str,
                               preferred_height: int, temp_dir: str,
                               allow_non_mp4_for_quality: bool,
                               existing_height: Optional[int]) -> bool:
    """
    Download YouTube -> temp; prefer exact preferred_height as MP4, fallback to HLS, progressive,
    and optionally ANY->MKV. Replace only if the new file is strictly better than existing_height,
    or if no existing trailer.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    # temp targets
    base_filename = os.path.basename(target_path)  # ends with .mp4 by builder
    temp_mp4 = os.path.join(temp_dir, base_filename)

    # clean stale temp
    try:
        if os.path.exists(temp_mp4):
            os.remove(temp_mp4)
    except Exception:
        pass

    h = preferred_height
    log.info(f"⇣ Downloading trailer (MP4-first) from {url} (target={h}p) → temp: {temp_mp4}")

    fmt_mp4_first = (
        f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height={h}]+bestaudio/"
        f"best[height={h}]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}]/"
        "best"
    )
    sort_mp4_bias = [f"res:{h}", "res", "ext:mp4:m4a", "codec:avc", "vbr", "abr"]

    # --- Try 1: MP4-first (DASH/HLS) ---
    try:
        _run_download(url, _common_ydl_opts(temp_mp4, temp_dir, fmt_mp4_first, sort_mp4_bias, h, "MP4 pass"))
        # Decide replace by probed height
        new_h = get_video_height(temp_mp4)
        log.info(f"• MP4 pass result height: {new_h if new_h else 'unknown'}")
        if existing_height is None or (new_h and new_h > existing_height):
            shutil.move(temp_mp4, target_path)
            log.info(f"✓ Placed (MP4): {target_path}")
            return True
        else:
            log.info(f"↺ Existing trailer is equal/better ({existing_height}p) → discard new MP4")
            try:
                if os.path.exists(temp_mp4):
                    os.remove(temp_mp4)
            except Exception:
                pass
            return True
    except DownloadError as e:
        msg = str(e)
        log.warning(f"MP4 pass failed: {msg}")

        # --- Try 2: HLS-only (m3u8) ---
        if _FRAG_403_RE.search(msg):
            log.info("→ Fallback: HLS-only (m3u8)")
            try:
                if os.path.exists(temp_mp4):
                    os.remove(temp_mp4)
                fmt_hls = (
                    f"bestvideo[protocol^=m3u8][height={h}]+bestaudio[protocol^=m3u8]/"
                    f"best[protocol^=m3u8][height={h}]/"
                    f"bestvideo[protocol^=m3u8][height<={h}]+bestaudio[protocol^=m3u8]/"
                    f"best[protocol^=m3u8][height<={h}]"
                )
                opts_hls = _common_ydl_opts(temp_mp4, temp_dir, fmt_hls, [f"res:{h}", "res", "vbr", "abr"], h, "HLS pass")
                opts_hls["extractor_args"] = {"youtube": {"player_client": ["web", "android", "ios"]}}
                _run_download(url, opts_hls)
                new_h = get_video_height(temp_mp4)
                log.info(f"• HLS pass result height: {new_h if new_h else 'unknown'}")
                if existing_height is None or (new_h and new_h > existing_height):
                    shutil.move(temp_mp4, target_path)
                    log.info(f"✓ Placed (HLS): {target_path}")
                    return True
                else:
                    log.info(f"↺ Existing trailer is equal/better ({existing_height}p) → discard new HLS")
                    try:
                        if os.path.exists(temp_mp4):
                            os.remove(temp_mp4)
                    except Exception:
                        pass
                    return True
            except Exception as e_hls:
                log.warning(f"HLS pass failed: {e_hls}")

        # --- Try 3: Progressive MP4 (<=720p sometimes) ---
        log.info("→ Fallback: progressive MP4")
        try:
            if os.path.exists(temp_mp4):
                os.remove(temp_mp4)
            fmt_prog = (
                f"best[ext=mp4][protocol^=http][height={h}]/"
                f"best[ext=mp4][protocol^=http][height<={h}]/"
                "best[ext=mp4][protocol^=http]/"
                "best[protocol^=http]"
            )
            opts_prog = _common_ydl_opts(temp_mp4, temp_dir, fmt_prog, [f"res:{h}", "res"], h, "Progressive MP4")
            opts_prog["extractor_args"] = {"youtube": {"player_client": ["web"]}}
            opts_prog["youtube_include_dash_manifest"] = False
            opts_prog["youtube_include_hls_manifest"] = False

            _run_download(url, opts_prog)
            new_h = get_video_height(temp_mp4)
            log.info(f"• Progressive pass result height: {new_h if new_h else 'unknown'}")
            if existing_height is None or (new_h and new_h > existing_height):
                shutil.move(temp_mp4, target_path)
                log.info(f"✓ Placed (progressive): {target_path}")
                return True
            else:
                log.info(f"↺ Existing trailer is equal/better ({existing_height}p) → discard progressive")
                try:
                    if os.path.exists(temp_mp4):
                        os.remove(temp_mp4)
                except Exception:
                    pass
                # maybe try ANY→MKV if allowed
        except Exception as e_prog:
            log.warning(f"Progressive MP4 failed: {e_prog}")

        # --- Try 4: ANY→MKV (for best quality, e.g., VP9/Opus) ---
        if allow_non_mp4_for_quality:
            log.info("→ Last resort: ANY→MKV")
            base_no_ext, _ = os.path.splitext(target_path)
            mkv_target = base_no_ext + ".mkv"
            temp_mkv = os.path.join(temp_dir, os.path.basename(mkv_target))
            try:
                if os.path.exists(temp_mkv):
                    os.remove(temp_mkv)
            except Exception:
                pass

            fmt_any = (
                f"bestvideo[height={h}]+bestaudio/"
                f"best[height={h}]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}]/"
                "best"
            )
            opts_any = _common_ydl_opts(temp_mkv, temp_dir, fmt_any, [f"res:{h}", "res", "vbr", "abr"], h, "MKV pass")
            opts_any["merge_output_format"] = "mkv"
            opts_any["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mkv"}]

            try:
                _run_download(url, opts_any)
                new_h2 = get_video_height(temp_mkv)
                log.info(f"• MKV pass result height: {new_h2 if new_h2 else 'unknown'}")

                # decide replacement vs existing
                current_best_h = existing_height
                current_best_path = None
                base_no_ext, _ = os.path.splitext(target_path)
                mp4_current = base_no_ext + ".mp4"
                mkv_current = base_no_ext + ".mkv"
                if os.path.exists(mkv_current):
                    current_best_path = mkv_current
                    current_best_h = get_video_height(mkv_current)
                elif os.path.exists(mp4_current):
                    current_best_path = mp4_current
                    current_best_h = get_video_height(mp4_current)

                if current_best_h is None or (new_h2 and new_h2 > current_best_h):
                    # remove old, move new MKV in place
                    try:
                        if current_best_path and os.path.exists(current_best_path):
                            os.remove(current_best_path)
                    except Exception:
                        pass
                    shutil.move(temp_mkv, mkv_target)
                    log.info(f"✓ Placed (MKV): {mkv_target}")
                    return True
                else:
                    log.info(f"↺ MKV not better than existing ({current_best_h if current_best_h else 'unknown'}p) → discard MKV")
                    try:
                        if os.path.exists(temp_mkv):
                            os.remove(temp_mkv)
                    except Exception:
                        pass
                    return True
            except Exception as e_any:
                log.warning(f"MKV pass failed: {e_any}")

    except Exception as e:
        log.warning(f"yt_dlp failed: {e}")

    # cleanup temp leftovers
    try:
        if os.path.exists(temp_mp4):
            os.remove(temp_mp4)
    except Exception:
        pass

    return False

# -------------------- Main flow --------------------

def parse_args():
    p = argparse.ArgumentParser(description="Manually fetch a trailer for a single movie directory from a YouTube URL/ID.")
    p.add_argument("movie_dir", help="Path to the movie directory")
    p.add_argument("youtube", help="YouTube URL or 11-char video ID")
    p.add_argument("--config", "-c", default="trailers.ini", help="Path to INI file (default: ./trailers.ini)")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)

    movie_dir = os.path.abspath(args.movie_dir)
    if not os.path.isdir(movie_dir):
        log.error(f"Not a directory: {movie_dir}")
        sys.exit(2)

    video_id = extract_youtube_id(args.youtube)
    if not video_id:
        log.error("Could not parse a valid YouTube video ID from the second argument.")
        sys.exit(2)

    movie_file = first_movie_file(movie_dir, cfg["video_exts"])
    if not movie_file:
        log.error("No movie file found in the given directory (check video_exts in INI).")
        sys.exit(2)

    # Build intended trailer target path (...-trailer.mp4)
    trailer_path = build_trailer_target_path(movie_dir, movie_file, cfg["trailer_suffix"])

    # Detect existing trailer (prefer .mkv over .mp4 when comparing)
    base_no_ext, _ = os.path.splitext(trailer_path)
    mp4_path = base_no_ext + ".mp4"
    mkv_path = base_no_ext + ".mkv"

    existing_path = None
    if os.path.exists(mkv_path):
        existing_path = mkv_path
    elif os.path.exists(mp4_path):
        existing_path = mp4_path

    existing_height = get_video_height(existing_path) if existing_path else None
    if existing_path and existing_height is not None:
        log.info(f"• Existing trailer: {existing_path} ({existing_height}p)")

    ok = download_youtube_to_manual(
        trailer_path,
        video_id,
        cfg["preferred_height"],
        cfg["temp_dir"],
        cfg["allow_non_mp4_for_quality"],
        existing_height=existing_height
    )

    if ok:
        log.info("✔ Done.")
        sys.exit(0)
    else:
        log.error("✗ Failed to download/replace trailer.")
        sys.exit(1)

if __name__ == "__main__":
    main()
