#!/usr/bin/env python3
import os
import re
import sys
import argparse
import configparser
import subprocess
from typing import Optional, Tuple, List

# ---------- Helpers ----------

TITLE_YEAR_PATTERNS = [
    re.compile(r"^(?P<title>.+?)\s*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]$", re.IGNORECASE),
    re.compile(r"^(?P<title>.+?)\s*[-–:,]\s*.+?\s*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]$", re.IGNORECASE),
    re.compile(r"^(?P<title>.+?)\s+(?P<year>19\d{2}|20\d{2})$", re.IGNORECASE),
]

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
    """Return video height (pixels) via ffprobe for the first video stream, or None if unknown."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "csv=p=0", path
        ], stderr=subprocess.STDOUT, text=True).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        return None
    return None

# ---------- Config ----------

def load_config(path: str) -> dict:
    cfg = configparser.ConfigParser()
    if not os.path.isfile(path):
        print(f"Config not found: {path}", file=sys.stderr)
        sys.exit(2)
    cfg.read(path)

    lang = cfg.get("settings", "language", fallback="de").strip().lower()
    strict_lang = cfg.getboolean("settings", "strict_language", fallback=False)
    video_exts_raw = cfg.get("settings", "video_exts", fallback="mkv,mp4,m4v,avi,mov")
    video_exts = {"."+e.strip().lower().lstrip(".") for e in video_exts_raw.split(",") if e.strip()}
    trailer_suffix = cfg.get("settings", "trailer_suffix", fallback="-trailer").strip()
    preferred_height = cfg.getint("settings", "preferred_height", fallback=1080)

    roots = []
    if "paths" in cfg:
        for _, v in cfg.items("paths"):
            v = v.strip()
            if v:
                roots.append(v)
    if not roots:
        print("No roots configured under [paths]. Add at least one directory.", file=sys.stderr)
        sys.exit(2)

    return {
        "language": lang,
        "strict_language": strict_lang,
        "video_exts": video_exts,
        "trailer_suffix": trailer_suffix,
        "preferred_height": preferred_height,
        "roots": roots,
    }

# ---------- Scanner ----------

def scan_stats(cfg: dict):
    total_movies = 0
    trailers_present = 0
    failing: List[Tuple[str, Optional[int], str]] = []   # (title, height, path)
    missing: List[str] = []                              # titles with no trailer

    for root in cfg["roots"]:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            movie_dir = os.path.join(root, d)
            if not os.path.isdir(movie_dir):
                continue

            movie_file = first_movie_file(movie_dir, cfg["video_exts"])
            if not movie_file:
                continue
            total_movies += 1

            trailer_mp4 = build_trailer_target_path(movie_dir, movie_file, cfg["trailer_suffix"])
            base_no_ext, _ = os.path.splitext(trailer_mp4)
            trailer_mkv = base_no_ext + ".mkv"

            candidates = []
            if os.path.exists(trailer_mp4):
                candidates.append(trailer_mp4)
            if os.path.exists(trailer_mkv):
                candidates.append(trailer_mkv)

            if not candidates:
                # completely missing
                title, year = extract_title_year_from_folder(os.path.basename(movie_dir))
                title_disp = f"{title} ({year})" if year else title
                missing.append(title_disp)
                continue

            # pick best by height
            best_path = None
            best_h: Optional[int] = None
            for p in candidates:
                h = get_video_height(p)
                if best_h is None or (h is not None and h > (best_h or -1)):
                    best_h = h
                    best_path = p

            trailers_present += 1

            if best_h is None or best_h < cfg["preferred_height"]:
                title, year = extract_title_year_from_folder(os.path.basename(movie_dir))
                title_disp = f"{title} ({year})" if year else title
                failing.append((title_disp, best_h, best_path or ""))

    return total_movies, trailers_present, failing, missing

# ---------- CLI ----------

def parse_args():
    p = argparse.ArgumentParser(description="Report trailer coverage and quality stats based on trailers.ini.")
    p.add_argument("--config", "-c", default="trailers.ini", help="Path to INI file (default: ./trailers.ini)")
    p.add_argument("--list-limit", type=int, default=0,
                   help="Limit how many failing/missing entries to list (0 = no limit)")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)

    total_movies, trailers_present, failing, missing = scan_stats(cfg)

    preferred = cfg["preferred_height"]
    print("")
    print("=== movie-trailer-downloader :: Stats ===")
    print(f"Roots: {', '.join(cfg['roots'])}")
    print(f"Language: {cfg['language']} | Preferred height: {preferred}p")
    print("")
    print(f"Total movie folders: {total_movies}")
    print(f"Trailers present   : {trailers_present} ({(trailers_present/total_movies*100.0):.1f}% coverage)" if total_movies else "Trailers present   : 0")
    print(f"Below target height: {len(failing)}")
    print(f"Completely missing : {len(missing)}")
    print("")

    if failing:
        print("=== Trailers below target height (or unknown) ===")
        limit = args.list_limit if args.list_limit and args.list_limit > 0 else len(failing)
        for i, (title_disp, h, path) in enumerate(failing[:limit], start=1):
            h_str = f"{h}p" if isinstance(h, int) else "unknown"
            print(f"{i:>3}. {title_disp:}  -> {h_str}  [{path}]")
        if limit < len(failing):
            print(f"... and {len(failing) - limit} more")
        print("")

    if missing:
        print("=== Movies with no trailer at all ===")
        limit = args.list_limit if args.list_limit and args.list_limit > 0 else len(missing)
        for i, title_disp in enumerate(missing[:limit], start=1):
            print(f"{i:>3}. {title_disp}")
        if limit < len(missing):
            print(f"... and {len(missing) - limit} more")
    else:
        print("All movies have at least one trailer.")

if __name__ == "__main__":
    main()
