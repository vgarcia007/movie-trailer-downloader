# movie-trailer-downloader

A simple Python tool to automatically download movie trailers in your preferred language and save them next to your local movie files.  
It uses [TMDB](https://www.themoviedb.org/) (for metadata and trailer links) and [YouTube](https://www.youtube.com/) (for fallback searches) combined with [yt-dlp](https://github.com/yt-dlp/yt-dlp) for downloading.  

## Motivation

I use the Arr suite to manage my media, but I was never satisfied with Trailarr or other methods to fetch matching trailers in my language. That’s why I built this script. No nonsense: no web interface, no Docker, just a plain Python script you can run on demand or via cron. It doesn’t matter where it runs, as long as it can access your movie folders. The only requirement is free API keys for YouTube and TMDB.


## Features
- Downloads trailers directly into your movie folders.
- Configurable language (e.g., German, English, French, …).
- Strict language mode (ignore TMDB trailers in the wrong language, but still try YouTube fallback in your chosen language).
- Works with multiple root directories.
- Configurable via simple `.ini` file.  
- Replace old trailer files if a higher resolution version is found

---

## Requirements
- Python 3.8+
- Installed system package: [ffmpeg](https://ffmpeg.org/)  
  ```bash
  sudo apt-get install ffmpeg
  ```
- Python dependencies (see `requirements.txt`):  
  ```bash
  pip3 install -r requirements.txt
  ```

---



## Configuration

You must provide your own configuration file `trailers.ini`.  
The repository contains an example: `trailers-example.ini`.  
Copy it and edit to fit your environment:

```bash
cp trailers-example.ini trailers.ini
```

### API Keys

This tool requires two API keys:  
- TMDB API key → Create a free account and generate a key here: https://www.themoviedb.org/settings/api  
- YouTube Data API key → Create a project in Google Cloud Console, enable the YouTube Data API v3, and generate an API key.

Both keys must be added to the [auth] section of your trailers.ini

### Example `trailers.ini`

```ini
[auth]
tmdb_api_key = xxx
youtube_api_key = xxx

[settings]
language = en
strict_language = true
video_exts = mkv, mp4, m4v, avi, mov
trailer_suffix = -trailer
preferred_height = 1080
allow_non_mp4_for_quality = true
temp_dir = ./tmp

[paths]
# All values in this section are used as search directories
root1 = /home/pi/NAS/media/filme
# root2 = /some/other/directory
```

---

## Example Movie Folder Layout

The tool assumes **one folder per movie**, with at least one video file inside.  
It will download the trailer into the same folder and name it after the movie file with `-trailer` appended.

```
/home/pi/NAS/media/filme/
├── Arielle die Meerjungfrau (1989)/
│   └── Arielle.die.Meerjungfrau.1989.German.1080p.mkv
│   └── Arielle.die.Meerjungfrau.1989.German.1080p-trailer.mp4
├── Butterfly Tale (2023)/
│   └── Butterfly.Tale.2023.German.1080p.mkv
│   └── Butterfly.Tale.2023.German.1080p-trailer.mp4
└── Zoomania (2016)/
    └── Zoomania.2016.German.1080p.mkv
    └── Zoomania.2016.German.1080p-trailer.mp4
```

---

## Usage

Run the script with your config:

```bash
python3 grab_trailers_ini.py --config trailers.ini
```

It will scan all root directories defined in `[paths]` and fetch trailers where missing.

---

## Notes
- If no trailer is found in your chosen language and `strict_language` is `false`, it may fall back to another available trailer.  
- If `strict_language` is `true`, only exact language matches will be downloaded.  

## License

This project is licensed under the WTFPL (Do What The Fuck You Want To Public License).
