#!/usr/bin/env python3
import os
import re
import sys
import time
import logging
import argparse
import configparser
import shutil
from typing import Optional, Tuple, List, Dict

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import subprocess


# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("trailers")

API_SLEEP = 0.25

LANG_MAP = {
    "de": ("de-DE", "DE", "Deutsch"),
    "en": ("en-US", "US", "English"),
    "fr": ("fr-FR", "FR", "Français"),
    "it": ("it-IT", "IT", "Italiano"),
    "es": ("es-ES", "ES", "Español"),
    "nl": ("nl-NL", "NL", "Nederlands"),
    "pt": ("pt-PT", "PT", "Português"),
    "pl": ("pl-PL", "PL", "Polski"),
    "tr": ("tr-TR", "TR", "Türkçe"),
    "ru": ("ru-RU", "RU", "Русский"),
}

TITLE_YEAR_PATTERNS = [
    re.compile(r"^(?P<title>.+?)\s*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]$", re.IGNORECASE),
    re.compile(r"^(?P<title>.+?)\s*[-–:,]\s*.+?\s*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]$", re.IGNORECASE),
    re.compile(r"^(?P<title>.+?)\s+(?P<year>19\d{2}|20\d{2})$", re.IGNORECASE),
]


# ---------- CONFIG LOADING ----------
def load_config(path: str):
    """Read INI file and return config dict."""
    cfg = configparser.ConfigParser()
    if not os.path.isfile(path):
        log.error(f"Config not found: {path}")
        sys.exit(2)
    cfg.read(path)

    # auth
    tmdb_key = cfg.get("auth", "tmdb_api_key", fallback=os.getenv("TMDB_API_KEY", "")).strip()
    yt_key = cfg.get("auth", "youtube_api_key", fallback=os.getenv("YT_API_KEY", "")).strip()
    if len(tmdb_key) < 10:
        log.error("Missing/invalid TMDB API key in [auth].")
        sys.exit(2)
    if len(yt_key) < 10:
        log.warning("No valid YouTube API key in [auth]. YouTube fallback may be limited.")

    # settings
    lang = cfg.get("settings", "language", fallback="de").strip().lower()
    strict_lang = cfg.getboolean("settings", "strict_language", fallback=False)
    video_exts_raw = cfg.get("settings", "video_exts", fallback="mkv,mp4,m4v,avi,mov")
    video_exts = {"."+e.strip().lower().lstrip(".") for e in video_exts_raw.split(",") if e.strip()}
    trailer_suffix = cfg.get("settings", "trailer_suffix", fallback="-trailer").strip()
    preferred_height = cfg.getint("settings", "preferred_height", fallback=1080)
    allow_non_mp4 = cfg.getboolean("settings", "allow_non_mp4_for_quality", fallback=True)

    temp_dir = cfg.get("settings", "temp_dir", fallback="/tmp/movie-trailer-downloader").strip()

    # paths: every value in [paths] is a root
    roots = []
    if "paths" in cfg:
        for _, v in cfg.items("paths"):
            v = v.strip()
            if v:
                roots.append(v)
    if not roots:
        log.error("No roots configured under [paths]. Add at least one directory.")
        sys.exit(2)

    return {
        "tmdb_api_key": tmdb_key,
        "youtube_api_key": yt_key,
        "language": lang,
        "strict_language": strict_lang,
        "video_exts": video_exts,
        "trailer_suffix": trailer_suffix,
        "roots": roots,
        "preferred_height": preferred_height,
        "allow_non_mp4_for_quality": allow_non_mp4,
        "temp_dir": temp_dir,
    }


# ---------- UTIL ----------
def normalize_title(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s

def extract_title_year_from_folder(folder_name: str) -> Tuple[str, Optional[int]]:
    raw = folder_name.strip()
    for pat in TITLE_YEAR_PATTERNS:
        m = pat.match(raw)
        if m:
            return normalize_title(m.group("title")), int(m.group("year"))
    m = re.search(r"(19\d{2}|20\d{2})", raw)
    year = int(m.group(1)) if m else None
    title = re.sub(r"[\(\[]?(19\d{2}|20\d{2})[\)\]]?", "", raw)
    title = re.sub(r"[-–,:]+$", "", title).strip()
    return normalize_title(title), year

def extract_title_year_from_filename(filename: str) -> Tuple[str, Optional[int]]:
    base, _ = os.path.splitext(filename)
    m = re.search(r"(19\d{2}|20\d{2})", base)
    year = int(m.group(1)) if m else None
    if m:
        base = base[:m.start()].strip()
    base = normalize_title(base)
    base = re.sub(r"(German|Deutsch|DL|EAC3|DTS|AC3|BluRay|WEB[- ]?DL|x265|x264|1080p|720p|2160p|UHD)$",
                  "", base, flags=re.IGNORECASE).strip()
    return base, year

def first_movie_file(path: str, video_exts: set) -> Optional[str]:
    files = [
        f for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f)) and os.path.splitext(f)[1].lower() in video_exts
    ]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getsize(os.path.join(path, f)), reverse=True)
    return files[0]

def build_trailer_target_path(movie_path: str, movie_filename: str, suffix: str) -> str:
    base, _ = os.path.splitext(movie_filename)
    return os.path.join(movie_path, f"{base}{suffix}.mp4")

def get_video_height(path: str) -> Optional[int]:
    """
    Return the height (in pixels) of the first video stream using ffprobe.
    Returns None if probing fails.
    """
    try:
        # -v error: only errors, -select_streams v:0: first video stream
        # -show_entries stream=height: print height, -of csv=p=0: plain number
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "csv=p=0", path
        ], stderr=subprocess.STDOUT, text=True).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    return None

# ---------- TMDB ----------
def tmdb_search_movie(title: str, year: Optional[int], tmdb_api_key: str, tmdb_locale: str) -> Optional[int]:
    params = {
        "api_key": tmdb_api_key,
        "language": tmdb_locale,
        "query": title,
        "include_adult": "false",
    }
    if year:
        params["year"] = year

    r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    results = data.get("results", [])
    if not results and year:
        params.pop("year", None)
        time.sleep(API_SLEEP)
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=15)
        results = r.json().get("results", [])

    if not results:
        return None

    def clean(t: str) -> str:
        return re.sub(r"\W+", "", t or "").lower()

    target = clean(title)
    results.sort(key=lambda x: (clean(x.get("title", "")) == target, x.get("popularity", 0.0)), reverse=True)
    return results[0].get("id")

def tmdb_trailer_youtube_key(movie_id: int, tmdb_api_key: str, lang_code: str) -> Tuple[Optional[str], Optional[str]]:
    params = {
        "api_key": tmdb_api_key,
        "language": f"{lang_code}-{lang_code.upper()}" if lang_code in LANG_MAP else "en-US",
        "include_video_language": lang_code,
    }
    r = requests.get(f"https://api.themoviedb.org/3/movie/{movie_id}/videos", params=params, timeout=15)
    if r.status_code != 200:
        return None, None
    vids = r.json().get("results", [])

    def is_trailer(v: Dict) -> bool:
        return v.get("site") == "YouTube" and v.get("type") == "Trailer"

    preferred = [v for v in vids if is_trailer(v) and v.get("iso_639_1") == lang_code]
    preferred.sort(key=lambda v: (bool(v.get("official")), "trailer" in (v.get("name", "").lower()), v.get("size", 0)), reverse=True)
    if preferred:
        v = preferred[0]
        return v.get("key"), v.get("iso_639_1")

    # Fallback: list all (no lang filter) and pick any trailer
    params_fb = {"api_key": tmdb_api_key, "language": "en-US"}
    r2 = requests.get(f"https://api.themoviedb.org/3/movie/{movie_id}/videos", params=params_fb, timeout=15)
    vids2 = r2.json().get("results", []) if r2.status_code == 200 else []
    any_trailer = [v for v in vids2 if is_trailer(v)]
    any_trailer.sort(key=lambda v: (v.get("iso_639_1") == lang_code, bool(v.get("official")), v.get("size", 0)), reverse=True)
    if any_trailer:
        v = any_trailer[0]
        return v.get("key"), v.get("iso_639_1")
    return None, None


# ---------- YouTube fallback ----------
def youtube_search_trailer(title: str, year: Optional[int], youtube_api_key: str, lang_code: str) -> Optional[str]:
    if not youtube_api_key:
        return None

    tmdb_locale, region, native_word = LANG_MAP.get(lang_code, ("en-US", "US", "English"))
    q = f"{title} {year} Trailer {native_word}" if year else f"{title} Trailer {native_word}"

    params = {
        "key": youtube_api_key,
        "part": "snippet",
        "type": "video",
        "maxResults": 6,
        "q": q,
        "relevanceLanguage": lang_code,
        "regionCode": region,
        "safeSearch": "none",
    }
    r = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=15)
    if r.status_code != 200:
        return None
    items = r.json().get("items", [])
    if not items:
        return None

    def score(item: Dict) -> tuple:
        s = item["snippet"]
        title_l = s.get("title", "").lower()
        ch_l = s.get("channelTitle", "").lower()
        lang_hit = any(k in title_l for k in [native_word.lower(), f"trailer {native_word.lower()}"])
        return ("trailer" in title_l, lang_hit, "trailer" in ch_l, s.get("publishedAt", ""))

    items.sort(key=score, reverse=True)
    return items[0]["id"]["videoId"]


# ---------- Download ----------
def download_youtube_to(
    target_path: str,
    video_id: str,
    preferred_height: int,
    temp_dir: str,
    allow_non_mp4_for_quality: bool,
    existing_height: Optional[int] = None
) -> bool:
    """
    Download into temp, prefer MP4; optionally retry with ANY→MKV for quality.
    Only replace the existing trailer if the new file's height is strictly greater
    than existing_height (or if no existing trailer).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    base_filename = os.path.basename(target_path)  # ...-trailer.mp4 (by builder)
    temp_mp4 = os.path.join(temp_dir, base_filename)

    def _hook(label):
        def inner(d):
            if d.get("status") == "finished":
                info = d.get("info_dict", {})
                h = info.get("height")
                ext = info.get("ext")
                vcodec = info.get("vcodec")
                acodec = info.get("acodec")
                fmt_id = info.get("format_id")
                log.info(f"✓ {label}: {h}p {ext} (v:{vcodec} a:{acodec}, id:{fmt_id})")
        return inner

    def probe_height(p: str) -> Optional[int]:
        return get_video_height(p)

    h = preferred_height
    format_str_mp4 = (
        f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height={h}]+bestaudio/"
        f"best[height={h}]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}]/"
        "best"
    )

    ydl_opts_mp4 = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "outtmpl": temp_mp4,
        "paths": {"temp": temp_dir},
        "format": format_str_mp4,
        "format_sort": [f"res:{h}", "res", "ext:mp4:m4a", "codec:avc", "vbr", "abr"],
        "format_sort_force": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": True,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "ignoreerrors": True,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "postprocessor_args": ["-movflags", "+faststart"],
        "progress_hooks": [_hook("MP4 pass")],
        "geo_bypass_country": "DE",
    }

    # Ensure no stale temp file
    try:
        if os.path.exists(temp_mp4):
            os.remove(temp_mp4)
    except Exception:
        pass

    log.info(f"⇣ Downloading trailer (MP4-first) from {url} (target={preferred_height}p) → temp: {temp_mp4}")

    # ---------- First pass: MP4 ----------
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL(ydl_opts_mp4) as ydl:
            ydl.download([url])

        new_h = probe_height(temp_mp4)
        if new_h:
            log.info(f"• MP4 pass result height: {new_h}p")
        else:
            log.info("• MP4 pass: could not probe height")

        # Decide whether to replace existing file
        if existing_height is None or (new_h and new_h > existing_height):
            # Replace/move into place; allow switching container extension if needed later
            shutil.move(temp_mp4, target_path)
            log.info(f"✓ Placed (MP4): {target_path}")
            # If we already hit preferred height, done.
            if new_h and new_h >= preferred_height:
                return True
            # Otherwise, we *might* upgrade further with MKV pass if allowed
        else:
            log.info(f"↺ Existing trailer is equal or better ({existing_height}p) → keep existing, discard MP4")
            try:
                if os.path.exists(temp_mp4):
                    os.remove(temp_mp4)
            except Exception:
                pass
            # No need to try MKV in this case
            return True

    except DownloadError as e:
        log.info("… MP4 pass failed or incomplete; evaluating MKV fallback")
    except Exception as e:
        log.warning(f"yt_dlp failed (MP4 pass) for {url}: {e}")

    # ---------- Second pass: ANY → MKV (optional) ----------
    if not allow_non_mp4_for_quality:
        # Clean temp mp4 if left
        try:
            if os.path.exists(temp_mp4):
                os.remove(temp_mp4)
        except Exception:
            pass
        return False

    mkv_target = os.path.splitext(target_path)[0] + ".mkv"
    temp_mkv = os.path.join(temp_dir, os.path.basename(mkv_target))

    format_str_any = (
        f"bestvideo[height={h}]+bestaudio/"
        f"best[height={h}]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/"
        "best"
    )

    ydl_opts_mkv = dict(ydl_opts_mp4)
    ydl_opts_mkv.update({
        "outtmpl": temp_mkv,
        "format": format_str_any,
        "format_sort": [f"res:{h}", "res", "vbr", "abr"],  # drop ext bias
        "merge_output_format": "mkv",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mkv"}],
        "progress_hooks": [_hook("MKV pass")],
    })

    # Ensure no stale temp mkv
    try:
        if os.path.exists(temp_mkv):
            os.remove(temp_mkv)
    except Exception:
        pass

    log.info(f"⇣ Retrying for quality (ANY→MKV) from {url} (target={preferred_height}p) → temp: {temp_mkv}")

    try:
        with YoutubeDL(ydl_opts_mkv) as ydl2:
            ydl2.download([url])

        new_h2 = probe_height(temp_mkv)
        if new_h2:
            log.info(f"• MKV pass result height: {new_h2}p")
        else:
            log.info("• MKV pass: could not probe height")

        # Compare against existing (or MP4 just placed)
        current_path = None
        # prefer current mkv/mp4 present at target base
        base_no_ext, _ = os.path.splitext(target_path)
        mp4_current = base_no_ext + ".mp4"
        mkv_current = base_no_ext + ".mkv"
        if os.path.exists(mkv_current):
            current_path = mkv_current
        elif os.path.exists(mp4_current):
            current_path = mp4_current

        current_h = get_video_height(current_path) if current_path else None

        if current_h is None or (new_h2 and new_h2 > current_h):
            # Remove the older current file if exists, then move MKV in place
            try:
                if current_path and os.path.exists(current_path):
                    os.remove(current_path)
            except Exception:
                pass
            shutil.move(temp_mkv, mkv_target)
            log.info(f"✓ Upgraded (MKV): {mkv_target}")
            return True
        else:
            log.info(f"↺ MKV not better than existing ({current_h or 'unknown'}p) → discard MKV")
            try:
                if os.path.exists(temp_mkv):
                    os.remove(temp_mkv)
            except Exception:
                pass
            return True

    except Exception as e2:
        log.warning(f"yt_dlp failed (MKV pass) for {url}: {e2}")
        try:
            if os.path.exists(temp_mkv):
                os.remove(temp_mkv)
        except Exception:
            pass
        return False

# ---------- Walk & Process ----------
def walk_movies(root_dir: str) -> List[str]:
    if not os.path.isdir(root_dir):
        log.warning(f"Root not found or not a directory: {root_dir}")
        return []
    return [
        os.path.join(root_dir, d)
        for d in sorted(os.listdir(root_dir))
        if os.path.isdir(os.path.join(root_dir, d))
    ]

def first_movie_file_in_dir(dir_path: str, video_exts: set) -> Optional[str]:
    return first_movie_file(dir_path, video_exts)

def process_movie_dir(movie_dir: str, cfg: dict) -> None:
    """
    Process a single movie folder:
    - detect existing trailer (mp4/mkv) and probe its height
    - if existing >= preferred_height: skip
    - else search TMDB (language-aware) then fall back to YouTube
    - download into temp dir and only replace if the new trailer has higher height
    """
    # 1) Find main movie file in this folder
    movie_file = first_movie_file_in_dir(movie_dir, cfg["video_exts"])
    if not movie_file:
        return

    # 2) Build target trailer path (defaults to ...-trailer.mp4)
    trailer_path = build_trailer_target_path(movie_dir, movie_file, cfg["trailer_suffix"])

    # 3) Detect existing trailer (prefer MKV over MP4, if both exist)
    base_no_ext, _ = os.path.splitext(trailer_path)  # points to ...-trailer
    mp4_path = base_no_ext + ".mp4"
    mkv_path = base_no_ext + ".mkv"

    existing_trailer_path = None
    if os.path.exists(mkv_path):
        existing_trailer_path = mkv_path
    elif os.path.exists(mp4_path):
        existing_trailer_path = mp4_path

    existing_height = None
    if existing_trailer_path:
        existing_height = get_video_height(existing_trailer_path)
        if existing_height is not None:
            log.info(f"• Existing trailer: {existing_trailer_path} ({existing_height}p)")
            if existing_height >= cfg["preferred_height"]:
                log.info(f"✓ Already at or above preferred height ({cfg['preferred_height']}p) → skip")
                return
            else:
                log.info(f"↻ Below preferred height ({cfg['preferred_height']}p) → will try to upgrade")
        else:
            log.info("• Existing trailer found but height unknown → will attempt upgrade if better is available")

    # 4) Extract title/year from folder (fallback to filename)
    folder_name = os.path.basename(movie_dir.rstrip(os.sep))
    title, year = extract_title_year_from_folder(folder_name)
    if len(title) < 2:
        t2, y2 = extract_title_year_from_filename(movie_file)
        title = t2 or title
        year = y2 or year

    # 5) Language/locale setup
    lang_code = cfg["language"]
    tmdb_locale, _, _ = LANG_MAP.get(lang_code, ("en-US", "US", "English"))

    log.info(f"→ Searching trailer for: '{title}' ({year or 'unknown'}) lang={lang_code}")

    # 6) TMDB first
    video_id = None
    found_lang = None
    movie_id = tmdb_search_movie(title, year, cfg["tmdb_api_key"], tmdb_locale)
    time.sleep(API_SLEEP)
    if movie_id:
        key, found_lang = tmdb_trailer_youtube_key(movie_id, cfg["tmdb_api_key"], lang_code)
        if key:
            video_id = key

    # 7) Strict handling: ignore TMDB result in wrong language (but log we will fallback)
    if cfg["strict_language"] and video_id and found_lang != lang_code:
        log.info(
            f"✗ TMDB trailer not in requested language ({found_lang}); "
            f"strict mode → ignoring TMDB result, will try YouTube fallback"
        )
        video_id = None

    # 8) YouTube fallback if needed
    if not video_id:
        log.info("→ Falling back to YouTube search…")
        time.sleep(API_SLEEP)
        video_id = youtube_search_trailer(title, year, cfg["youtube_api_key"], lang_code)

    if not video_id:
        log.warning(f"✗ No trailer found for '{title}' in lang={lang_code}")
        return

    # 9) Download to temp and conditionally replace only if it improves height
    ok = download_youtube_to(
        trailer_path,
        video_id,
        cfg["preferred_height"],
        cfg["temp_dir"],
        cfg["allow_non_mp4_for_quality"],
        existing_height=existing_height,
    )

    if ok:
        log.info(f"✔ Done: {trailer_path if os.path.exists(trailer_path) else (base_no_ext + '.mkv')}")
    else:
        log.warning(f"✗ Download failed for '{title}'")



# ---------- Main ----------
def parse_args():
    p = argparse.ArgumentParser(description="Download trailers next to movie files (INI-configurable).")
    p.add_argument("--config", "-c", default="trailers.ini", help="Path to INI file (default: ./trailers.ini)")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)

    log.info(
        "Config loaded:\n"
        f"  language={cfg['language']}  strict={cfg['strict_language']}\n"
        f"  exts={','.join(sorted(cfg['video_exts']))}\n"
        f"  suffix='{cfg['trailer_suffix']}'\n"
        f"  roots={cfg['roots']}\n"
    )

    for root in cfg["roots"]:
        for movie_dir in walk_movies(root):
            try:
                process_movie_dir(movie_dir, cfg)
            except KeyboardInterrupt:
                log.info("Interrupted by user.")
                sys.exit(130)
            except Exception as e:
                log.warning(f"Error in '{movie_dir}': {e}")
            finally:
                time.sleep(API_SLEEP)

if __name__ == "__main__":
    main()
