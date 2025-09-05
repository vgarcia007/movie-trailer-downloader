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
def download_youtube_to(target_path: str, video_id: str, preferred_height: int, temp_dir: str) -> bool:
    """
    Download into a dedicated temp directory, then move the finished file
    into place using shutil.move (copy+remove across devices).
    Avoids .part files in watched folders and prevents nested tmp paths.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    temp_filename = os.path.basename(target_path)
    temp_out = os.path.join(temp_dir, temp_filename)

    # ensure no stale temp file from previous runs
    try:
        if os.path.exists(temp_out):
            os.remove(temp_out)
    except Exception:
        pass

    log.info(f"⇣ Downloading trailer from {url} (target={preferred_height}p) → temp: {temp_out}")

    def _hook(d):
        if d.get("status") == "finished":
            info = d.get("info_dict", {})
            height = info.get("height")
            ext = info.get("ext")
            vcodec = info.get("vcodec")
            acodec = info.get("acodec")
            fmt_id = info.get("format_id")
            log.info(f"✓ Final format: {height}p {ext} (v:{vcodec} a:{acodec}, id:{fmt_id})")

    h = preferred_height
    format_str = (
        f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height={h}]+bestaudio/"
        f"best[height={h}]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}]/"
        "best"
    )

    ydl_opts = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,

        # write final file directly into our temp dir (absolute path!)
        "outtmpl": temp_out,

        # route only fragments/.part into temp; DO NOT set "home" to avoid nested tmp paths
        "paths": {"temp": temp_dir},

        "format": format_str,
        "format_sort": [f"res:{h}", "res", "ext:mp4:m4a", "codec:avc", "vbr", "abr"],
        "format_sort_force": True,

        # SABR workaround
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": True,

        # robustness
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "ignoreerrors": True,

        # output as mp4
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "postprocessor_args": ["-movflags", "+faststart"],

        "progress_hooks": [_hook],
        "geo_bypass_country": "DE",
    }

    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        shutil.move(temp_out, target_path)
        log.info(f"✓ Moved into place: {target_path}")
        return True

    except DownloadError as e:
        # retry with web client only
        try:
            alt_opts = dict(ydl_opts)
            alt_opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
            with YoutubeDL(alt_opts) as ydl2:
                ydl2.download([url])
            shutil.move(temp_out, target_path)
            log.info(f"✓ Moved into place: {target_path}")
            return True
        except Exception:
            log.warning(f"yt_dlp failed (alt client) for {url}: {e}")
            try:
                if os.path.exists(temp_out):
                    os.remove(temp_out)
            except Exception:
                pass
            return False

    except Exception as e:
        log.warning(f"yt_dlp failed for {url}: {e}")
        try:
            if os.path.exists(temp_out):
                os.remove(temp_out)
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
    movie_file = first_movie_file_in_dir(movie_dir, cfg["video_exts"])
    if not movie_file:
        return

    trailer_path = build_trailer_target_path(movie_dir, movie_file, cfg["trailer_suffix"])
    if os.path.exists(trailer_path) and os.path.getsize(trailer_path) > 0:
        log.info(f"✓ Trailer already exists: {trailer_path}")
        return

    folder_name = os.path.basename(movie_dir.rstrip(os.sep))
    title, year = extract_title_year_from_folder(folder_name)
    if len(title) < 2:
        t2, y2 = extract_title_year_from_filename(movie_file)
        title = t2 or title
        year = y2 or year

    lang_code = cfg["language"]
    tmdb_locale, _, _ = LANG_MAP.get(lang_code, ("en-US", "US", "English"))
    log.info(f"→ Searching trailer for: '{title}' ({year or 'unknown'}) lang={lang_code}")

    # TMDB first
    video_id = None
    found_lang = None
    movie_id = tmdb_search_movie(title, year, cfg["tmdb_api_key"], tmdb_locale)
    time.sleep(API_SLEEP)
    if movie_id:
        key, found_lang = tmdb_trailer_youtube_key(movie_id, cfg["tmdb_api_key"], lang_code)
        if key:
            video_id = key

    # Strict language handling
    if cfg["strict_language"] and video_id and found_lang != lang_code:
        log.info(
            f"✗ TMDB trailer not in requested language ({found_lang}); "
            f"strict mode → ignoring TMDB result, will try YouTube fallback"
        )
        video_id = None

    # YouTube fallback
    if not video_id:
        log.info("→ Falling back to YouTube search…")
        time.sleep(API_SLEEP)
        video_id = youtube_search_trailer(title, year, cfg["youtube_api_key"], lang_code)

    if not video_id:
        log.warning(f"✗ No trailer found for '{title}' in lang={lang_code}")
        return

    ok = download_youtube_to(trailer_path, video_id, cfg["preferred_height"], cfg["temp_dir"])
    if ok:
        log.info(f"✔ Saved trailer: {trailer_path}")
    else:
        try:
            if os.path.exists(trailer_path) and os.path.getsize(trailer_path) == 0:
                os.remove(trailer_path)
        except Exception:
            pass
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
