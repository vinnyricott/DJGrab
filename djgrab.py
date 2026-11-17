#!/usr/bin/env python3
"""
DJ Grab engine: link -> best available audio -> MP3 320 with clean tags for Serato / Pulse DJ.

Usage:
  djgrab.py URL [URL ...] --out "~/DJ music/DJ Grab" [--json] [--cookies-browser chrome]
  djgrab.py --update            (update yt-dlp)

With --json, every status line printed to stdout is a JSON object (used by the Mac app).
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import platform
WINDOWS = platform.system() == "Windows"
APP_SUPPORT = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DJ Grab" if WINDOWS
               else Path.home() / "Library" / "Application Support" / "DJGrab")
LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "pcm_s16le", "pcm_s24le", "pcm_s32le",
                   "pcm_s16be", "pcm_s24be", "pcm_f32le", "pcm_f32be"}

JSON_MODE = False


def emit(event, **data):
    """Report status. JSON for the app, readable text for Terminal."""
    if JSON_MODE:
        print(json.dumps({"event": event, **data}), flush=True)
    else:
        if event == "progress":
            print(f"\r  {data.get('stage', '')} {data.get('pct', 0):5.1f}%", end="", flush=True)
        elif event == "item_start":
            print(f"\n> {data.get('name', data.get('url'))}")
        elif event == "done":
            print(f"\r  saved: {data['path']}")
            if data.get("crate"):
                print(f"  crate: {data['crate']} ({data.get('crate_status')})")
            for w in data.get("warnings", []):
                print(f"  ! {w}")
        elif event == "checked":
            print(f"\r  {data['name']}: {data['message']}")
        elif event == "match" and data.get("matches"):
            print(f"\r  you already have: {', '.join(data['matches'])}")
        elif event in ("error", "skipped", "info", "crates"):
            print(f"\r  {event}: {data.get('message', '')}")


# --------------------------------------------------------------------------- tools

def find_tool(name):
    exe = name + (".exe" if WINDOWS else "")
    own = str(APP_SUPPORT / "bin" / exe)                                     # the app's own copy
    for p in (own, shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if p and os.path.exists(p):
            return p
    raise SystemExit(f"{name} not found. Open DJ Grab again and let it finish setting up.")


FFMPEG = None
FFPROBE = None


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# --------------------------------------------------------------------------- names

# Bracketed bits that are video/upload noise. Things DJs care about
# (remix, edit, extended, clean, dirty, intro, acapella, VIP, live...) are kept.
JUNK_WORDS = [
    r"official\s*(music\s*)?video", r"official\s*(hd\s*)?video", r"official\s*audio",
    r"official\s*lyrics?\s*video", r"official\s*visuali[sz]er", r"official",
    r"lyrics?\s*video", r"lyrics?", r"visuali[sz]er", r"music\s*video", r"video\s*oficial",
    r"audio\s*oficial", r"audio", r"video", r"m/?v", r"hd", r"hq", r"4k", r"1080p", r"720p",
    r"out\s*now", r"free\s*(dl|download)", r"premiere", r"exclusive",
    r"full\s*song", r"high\s*quality", r"color\s*coded.*", r"with\s*lyrics",
    r"remaster(ed)?(\s*(audio|version|\d{4}))*(\s*hq)?", r"(\d{4}\s*)?remaster(ed)?", r"(official\s*)?audio\s*hq", r"hq\s*audio",
    r"[^\)\]]*soundtrack[^\)\]]*", r"lyrics?\s*sub[^\)\]]*", r"lyrics?\s*/\s*letra", r"letra", r"subtitulad[oa]",
]
JUNK_RE = re.compile(r"\s*[\(\[\{]\s*(?:" + "|".join(JUNK_WORDS) + r")\s*[\)\]\}]", re.I)
LEADING_JUNK_RE = re.compile(r"^\s*(?:[\[\(]?\s*(premiere|free\s*(dl|download)|new)\s*[\]\)]?\s*[:|\-]?\s*)+", re.I)
TRAILING_PIPE_RE = re.compile(r"\s+(\||//|•)\s+.*$")
TRAILING_DASH_JUNK_RE = re.compile(r"(\s+-\s+(lyrics?|audio|official[^()]*|hq|\d{4}|remaster(ed)?[^()]*))+\s*$", re.I)
FEAT_RE = re.compile(r"\b(?:ft\.?|feat\.?|featuring)\s+", re.I)
SEPARATORS = [" - ", " – ", " — ", " -- ", " ~ "]


def tidy(s):
    s = s.replace("\u200b", "").replace("＂", '"')
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\(\s*\)|\[\s*\]", "", s).strip()
    return s.strip(" -–—|").strip()


def normalize_feat(s):
    return FEAT_RE.sub("feat. ", s)


def wrap_loose_feat(title):
    """'Song feat. X (Remix)' -> 'Song (feat. X) (Remix)'"""
    m = re.search(r"\s(feat\. [^\(\)\[\]]+?)(?=\s*[\(\[]|$)", title)
    if m and not re.search(r"\(\s*feat\.", title):
        title = title[:m.start()] + f" ({m.group(1).strip()})" + title[m.end():]
    return title


def clean_channel(name):
    if not name:
        return ""
    name = re.sub(r"\s*-\s*Topic$", "", name)
    if re.search(r"VEVO$", name):
        name = re.sub(r"VEVO$", "", name)
        if re.fullmatch(r"(?:[A-Z][a-z0-9]+)+", name):  # TaylorSwift -> Taylor Swift
            name = re.sub(r"(?<!^)(?=[A-Z])", " ", name)
    name = re.sub(r"\s*(official|music)$", "", name, flags=re.I)
    return tidy(name)


def strip_quotes(s):
    s = s.strip()
    for a, b in (('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’")):
        if len(s) > 2 and s.startswith(a) and s.endswith(b):
            return s[1:-1].strip()
    # 'Song "Name" (Remix)' style where the name is quoted at the front
    m = re.match(r'^["“](.+?)["”](.*)$', s)
    if m:
        return (m.group(1) + m.group(2)).strip()
    return s


def parse_names(info):
    """Return (artist, title) cleaned for a DJ library."""
    raw_title = info.get("track") or ""
    artist = ""
    if info.get("artists"):
        artist = ", ".join(info["artists"])
    elif info.get("artist"):
        artist = info["artist"]

    if raw_title and artist:  # proper music metadata (YouTube Music / Topic / some SoundCloud)
        title = raw_title
    else:
        title = info.get("title") or "Unknown"
        title = TRAILING_PIPE_RE.sub("", title)
        title = LEADING_JUNK_RE.sub("", title)
        split = None
        for sep in SEPARATORS:
            if sep in title:
                split = title.split(sep, 1)
                break
        if split:
            artist, title = split[0], split[1]
        else:
            artist = artist or clean_channel(info.get("uploader") or info.get("channel") or "")

    title = TRAILING_DASH_JUNK_RE.sub("", title)
    title = re.sub(r"\s+HQ$", "", title)
    for _ in range(3):  # junk can be stacked: "(Official Video) [HD]"
        title = JUNK_RE.sub("", title)
        artist = JUNK_RE.sub("", artist)
    title = strip_quotes(tidy(title))
    artist = strip_quotes(tidy(artist))
    title = wrap_loose_feat(normalize_feat(title))
    artist = normalize_feat(artist)
    # Normalise bracket style: [Remix] -> (Remix)
    title = re.sub(r"\[([^\[\]]+)\]", r"(\1)", title)
    artist, title, _ = polish(tidy(artist), tidy(title))
    real = title_from_artists(artist, title)          # uploaded as just "Artist & Artist": find the real title
    if real:
        artist, title = title, real
    return tidy(artist) or "Unknown Artist", tidy(title) or "Unknown Title"



# Second pass over a finished "Artist - Title": catches upload junk that isn't in brackets,
# channel names in front of the artist, and artist/title the wrong way round.
UPLOAD_CHANNELS = {"7clouds", "7clouds rock", "vibe music", "dark city sounds", "cleansongs", "doong doong",
                   "dreams music", "bemu", "blanc", "trending songs", "skyline95100", "popular", "eleven",
                   "zilmusic", "mgc-9", "beegees", "alyankovic", "old skool rewind official", "darling",
                   "countryhits", "lyrics", "letralyrics", "trap nation", "proximity", "mrsuicidesheep"}
COVER_RE = re.compile(r"\b(studio crew|tribute band|made famous|in the style of|originally performed|karaoke)\b"
                      r"|\(\s*cover\s*\)|\bcover version\b", re.I)
LIVE_RE = re.compile(r"\b(back ?stage at|festival|live at|live from|live on|live in|recorded at|fan ?cam|in concert|"
                     r"session|sessions|kexp|tiny desk|npr|audiotree|colors show|a colors|bbc radio|live lounge|"
                     r"unplugged|acoustic version|on the road)\b", re.I)
# Channels that only post live performances and studio sessions (never the record)
LIVE_CHANNELS = re.compile(r"\b(kexp|npr music|audiotree|colors|bbc radio ?1|live lounge|sofar|jam in the van|"
                           r"la blogotheque|vevo dscvr|mahogany|the late show|tonight show|jimmy kimmel|snl)\b", re.I)
_TAIL_JUNK = re.compile(r"(?:\s+-\s+(?:\d{4}|pop|rock|hq|hd|audio|lyrics?|official.*)"
                        r"|\s+(?:hq|hd|audio hq|hq audio|relaid audio|audio|lyrics?|letra ?lyrics?|video oficial|"
                        r"official( music| lyrics?)? video( hd| \d{4})?|original video|clip video|\d{5,6}))+\s*$", re.I)


def _known(a):
    return primary_artist(a) in artist_genres()


def title_from_artists(artist, title):
    """An upload titled with only the artists' names ("Chris Lorenzo & Kah-Lo"): find the song's real title in
    the music catalog (the newest track by those artists). Returns the title, or None."""
    words = lambda x: set(re.findall(r"[a-z0-9]+", x.lower())) - {"feat", "ft", "x", "and", "with", "vs"}
    tw, aw = words(title or ""), words(artist or "")
    # the title holds the artist's own name and at most a few more words joined by "&" / "x" / "feat."
    if not tw or not aw or not aw <= tw or not re.search(r"&|\bx\b|\bfeat|\bft\b|,", title or "", re.I):
        return None
    names = [n.strip() for n in re.split(r"\s*(?:&|,|\bx\b|\bfeat\.?|\bft\.?|\band\b)\s*", title, flags=re.I) if n.strip()]
    if len(names) < 2:
        return None
    import urllib.parse
    try:
        hits = _get_json("https://api.deezer.com/search?limit=25&q=" + urllib.parse.quote(" ".join(names))).get("data") or []
    except Exception:
        return None
    first = names[0].lower()
    for h in hits:
        t = (h.get("title_short") or h.get("title") or "").strip()
        if (h.get("artist") or {}).get("name", "").lower() == first and t and not words(t) <= words(title):
            return t
    return None


def polish(artist, title):
    """-> (artist, title, problem) where problem is None, 'cover' or 'live'."""
    a, t = artist, title
    # emoji / hearts and anything after them ("❤ Love Songs"), hashtags, @handles
    t = re.sub(r"\s*[☀-➿\U0001F300-\U0001FAFF].*$", "", t)
    t = re.sub(r"\s*\([^()]*(?:@\S+|\.com)[^()]*\)", "", t)              # "(follow @x)", "(site.com)"
    t = re.sub(r"\s+by\s+@\S+", "", t, flags=re.I)
    t = re.sub(r"\s+[#@]\S+", "", t)
    t = re.sub(r"\s+-\s+official[^()]*\)", ")", t, flags=re.I)            # "(X Remix - Official Video)"
    t = re.sub(r"\s*\(\s*letra\s*lyrics?\s*\)\s.*$", "", t, flags=re.I)   # "(LetraLyrics) <album>"
    t = re.sub(r"\s+lyric video$", "", t, flags=re.I)
    t = re.sub(r"\s*\(\s*\d{3}°?\s*visuali[sz]er\s*\).*$", "", t, flags=re.I)   # "(360° Visualizer) <album>"
    # bracketed junk the first pass misses
    t = re.sub(r"\s*\((?:remastered audio|audio hq|remastered|audio)\)", "", t, flags=re.I)
    t = re.sub(r"\(audio,\s*", "(", t, flags=re.I)
    t = re.sub(r"\s*\(\s*(?:\d{3}°?\s*visuali[sz]er|clip video|original video|lyricssub[^)]*|"
               r"letra\s*lyrics?|letralyrics|letra lyric|[^()]*\b(?:records|recordings))\s*\)", "", t, flags=re.I)
    t = re.sub(r"\s+lyrics\)", ")", t, flags=re.I)                     # "(feat. X Lyrics)"
    t = re.sub(r"\s*\(\s*\)", "", t)
    for _ in range(3):
        t = _TAIL_JUNK.sub("", t)
    t = re.sub(r"\s+Original Mix$", " (Original Mix)", t)
    # "Channel - Artist - Title": the real artist is inside the title
    if " - " in t:
        first, rest = t.split(" - ", 1)
        if a.lower() in UPLOAD_CHANNELS or (_known(first) and not _known(a)):
            a, t = first, rest
    # "Title - Artist": swap when only the right-hand side is a known artist
    if not _known(a) and _known(t) and not re.search(r"[()]", t):
        a, t = t, a
    # "Title - NOLA Bounce" style version tags -> brackets
    m = re.match(r"^(.*?)\s+-\s+(.*\b(?:remix|edit|bounce|mix|refix|redrum|version|rework|flip|bootleg))$", t, re.I)
    if m:
        t = f"{m.group(1)} ({m.group(2)})"
    problem = None
    blob = f"{artist} {title}"
    mby = re.search(r"\bby\s+([^()]+)$", title, re.I)
    if mby and not re.search(r"prod\.?\s*by|dir\.?\s*by", title, re.I) and _known(mby.group(1)) and not _known(artist):
        problem = "cover"                                             # "Cruise by Florida Georgia Line"
    if COVER_RE.search(blob) or primary_artist(artist) in ("7th heaven",):
        problem = "cover"
    if LIVE_RE.search(blob) or re.search(r"\s\d{5,6}$", title):
        problem = problem or "live"
    return tidy(a), tidy(t), problem


def safe_filename(s):
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    s = re.sub(r"\s+", " ", s).strip(". ")
    return s[:180] or "Untitled"


# --------------------------------------------------------------------------- audio

def probe_audio(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample,bit_rate,channels",
             "-of", "json", str(path)])
    try:
        return json.loads(r.stdout)["streams"][0]
    except Exception:
        return {}


NORMALIZE = 0.0     # target loudness in LUFS (0 = off), from --normalize


def to_mp3(src, dst, stream):
    codec = (stream.get("codec_name") or "").lower()
    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    if codec == "mp3":
        # Already MP3: copy it bit for bit. Re-encoding would only add loss and size.
        audio = ["-c:a", "copy"]
        mode = "copied"
    else:
        # 320 kbps CBR, highest LAME quality. From a ~130-160 kbps stream this adds
        # nothing audible. Sample rate is kept (no resampling).
        audio = ["-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0"]
        mode = "mp3 320"
    # dead air at the start (YouTube uploads often have a few silent seconds before the music): cut it,
    # keeping a hair (20 ms) before the first sound
    lead = 0.0
    try:
        sd = run([FFMPEG, "-v", "info", "-t", "30", "-i", str(src), "-af", "silencedetect=n=-55dB:d=0.3",
                  "-f", "null", "-"])
        m = re.search(r"silence_start: (-?[\d.]+)\s.*?silence_end: ([\d.]+)", sd.stderr, re.S)
        if m and float(m.group(1)) <= 0.05:
            lead = max(0.0, float(m.group(2)) - 0.02)
    except Exception:
        lead = 0.0
    trim = ["-ss", f"{lead:.3f}"] if lead > 0.3 else []
    # volume normalizing: every song to the same loudness, by turning it up or down only (no limiter, so the
    # sound isn't squashed). Louder than the target: turned down. Quieter: turned up only as far as its
    # loudest peak allows (1 dB below clipping), so a very dynamic song can stay a little under the target.
    gain_db = 0.0
    if NORMALIZE:
        try:
            l0, p0 = measure_loudness(src)
            if l0 is not None:
                gain_db = NORMALIZE - l0
                if p0 is not None:
                    gain_db = min(gain_db, -1.0 - p0)
        except Exception:
            gain_db = 0.0
    af = ["-af", f"volume={gain_db:.2f}dB"] if abs(gain_db) >= 0.5 else []
    if af and audio == ["-c:a", "copy"]:          # an MP3 has to be re-encoded to change its volume
        audio = ["-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0"]
        mode = "mp3 320"
    cmd = [FFMPEG, "-y", "-v", "error", *trim, "-i", str(src), "-map", "0:a:0", "-vn", *af,
           "-map_metadata", "-1", *audio, "-id3v2_version", "0", "-write_xing", "1", str(dst)]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg conversion failed: " + r.stderr.strip()[-300:])
    return {"source_codec": codec, "source_kbps": round(int(stream.get("bit_rate") or 0) / 1000),
            "lossless_source": lossless, "mode": mode, "gain_db": round(gain_db, 1) if af else 0.0}


def measure_loudness(path):
    """Integrated loudness (LUFS) and true peak (dBTP) via ffmpeg's EBU R128 meter."""
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
             "-af", "ebur128=peak=true", "-f", "null", "-"])
    text = r.stderr
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    lufs = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    peak = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary, re.S)

    def num(m):
        if not m or "inf" in m.group(1):
            return None
        return float(m.group(1))
    return num(lufs), num(peak)


def loudness_warnings(lufs, peak):
    """Only warn when it will be a real problem. Normal loud masters overshoot 0 dBTP by a
    couple of dB after lossy encoding. That's inaudible in Serato and gone once you trim
    down, so it isn't flagged. The numbers are still saved in the Comment field."""
    warnings = []
    if lufs is not None and lufs < -20:
        warnings.append(f"Unusually quiet ({lufs:.1f} LUFS). Might be a bad rip or a live recording")
    return warnings


def official_cover(artist, title, out_jpg):
    """The song's real album (or single) artwork, never a video frame. Deezer first (1000 px), Apple Music's
    catalog as the backup (1000 px). Used only when the catalog's artist AND title match the song; an
    album from a compilation ("Now That's What I Call...") is passed over for the artist's own release.
    Returns True when artwork was saved."""
    import urllib.parse, urllib.request
    if not artist or not title:
        return False
    a = primary_artist(artist)
    t = re.sub(r"\s*[\(\[].*$", "", title).strip() or title
    norm = lambda x: re.sub(r"[^a-z0-9]+", "", fold(re.sub(r"\s*[\(\[].*$", "", x or "")))
    na, nt = norm(a), norm(t)
    urls = []
    try:
        q = urllib.parse.quote(f'artist:"{a}" track:"{t}"')
        hits = _get_json(f"https://api.deezer.com/search?q={q}&limit=15").get("data") or []
        good = [h for h in hits if norm(h.get("title")) == nt and norm((h.get("artist") or {}).get("name")) == na]
        good.sort(key=lambda h: bool(re.search(r"hits|now that|best of|greatest|compilation|various",
                                               (h.get("album") or {}).get("title", ""), re.I)))
        urls += [(h.get("album") or {}).get("cover_xl") for h in good[:3]]
    except Exception:
        pass
    if not any(urls):
        try:
            q = urllib.parse.quote(f"{a} {t}")
            res = _get_json(f"https://itunes.apple.com/search?entity=song&limit=15&term={q}").get("results") or []
            good = [r for r in res if norm(r.get("trackName")) == nt and norm(r.get("artistName")) == na
                    and r.get("collectionArtistName", "").lower() != "various artists"]
            urls += [r.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb") for r in good[:3]]
        except Exception:
            pass
    for u in [u for u in urls if u]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "DJGrab/1.0"})
            data = urllib.request.urlopen(req, timeout=15).read()
            if len(data) > 5000 and data[:3] == b"\xff\xd8\xff":          # a real JPEG, not an error page
                Path(out_jpg).write_bytes(data)
                return True
        except Exception:
            continue
    return False




def write_tags(path, meta, cover):
    from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON, COMM,
                             APIC, WOAS, TXXX, TPE2)
    try:
        t = ID3(str(path))
    except ID3NoHeaderError:
        t = ID3()
    t.add(TIT2(encoding=3, text=meta["title"]))
    t.add(TPE1(encoding=3, text=meta["artist"]))
    t.add(TPE2(encoding=3, text=meta["artist"]))
    if meta.get("album"):
        t.add(TALB(encoding=3, text=meta["album"]))
    if meta.get("year"):
        t.add(TDRC(encoding=3, text=str(meta["year"])))
    if meta.get("genre"):
        t.add(TCON(encoding=3, text=meta["genre"]))
    if meta.get("source_note"):
        t.add(TXXX(encoding=3, desc="DJ Grab source", text=meta["source_note"]))
    if meta.get("genre_code"):
        t.add(TXXX(encoding=3, desc="DJ Grab genre", text=meta["genre_code"]))
    try:
        set_comment(t, song_comment(meta["artist"], meta["title"], meta.get("year")))
    except Exception:
        pass
    if meta.get("url"):
        t.add(WOAS(url=meta["url"]))
        t.add(TXXX(encoding=3, desc="Source", text=meta["url"]))
    if cover and Path(cover).exists():
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                   data=Path(cover).read_bytes()))
    t.save(str(path), v2_version=3)  # ID3v2.3 is what Serato reads most reliably


# --------------------------------------------------------------------------- genre sorting
#
# When the save folder sits next to the genre folders (Hip Hop, Pop, Caribbean...), each
# track is filed into its genre folder. The artist list comes from the library cleanup;
# unknown artists are looked up online (Deezer, then MusicBrainz) and remembered.

GENRE_PATH = {"HR": "Hip Hop/Rap", "HD": "Hip Hop/Drill", "HB": "Hip Hop/Bounce", "HO": "Hip Hop/Old School",
    "RB": "R&B/R&B", "RC": "R&B/Classic R&B & Soul", "RF": "R&B/Funk & Disco",
    "PP": "Pop/Pop", "PC": "Pop/Classic Pop", "PK": "Pop/K-Pop",
    "DH": "House/Classic House", "UT": "House/Tech House", "UA": "House/Afro House", "UM": "House/Melodic House",
    "UL": "House/Latin House", "UF": "House/Disco & Funky House", "UB": "House/Bass & UK House", "UC": "House/Commercial House",
    "DE": "Dance & Electronic/EDM", "DB": "Dance & Electronic/Dubstep & Bass",
    "DC": "Dance & Electronic/Dance Classics", "JC": "Club/Jersey Club",
    "CD": "Caribbean/Dancehall", "CS": "Caribbean/Soca", "CR": "Caribbean/Reggae", "CZ": "Caribbean/Zess", "CK": "Caribbean/Kompa",
    "AA": "African/Afrobeats", "AP": "African/Amapiano",
    "LR": "Latin/Reggaeton", "LP": "Latin/Latin Pop", "LT": "Latin/Tropical", "LB": "Latin/Brazilian Funk",
    "RK": "Rock/Rock", "RM": "Rock/Metal", "RA": "Rock/Alternative & Indie", "CO": "Country"}
GENRE_NAME = {"HR": "Hip Hop", "HD": "Drill", "HB": "Bounce", "HO": "Old School Hip Hop", "RB": "R&B",
    "RC": "Classic R&B", "RF": "Funk & Disco", "PP": "Pop", "PC": "Classic Pop", "PK": "K-Pop", "DH": "Classic House", "UT": "Tech House", "UA": "Afro House", "UM": "Melodic House",
    "UL": "Latin House", "UF": "Disco House", "UB": "Bass House", "UC": "House",
    "DE": "EDM", "DB": "Dubstep", "DC": "Dance Classics", "JC": "Jersey Club", "CD": "Dancehall", "CS": "Soca",
    "CR": "Reggae", "CZ": "Zess", "CK": "Kompa", "AA": "Afrobeats", "AP": "Amapiano", "LR": "Reggaeton",
    "LP": "Latin Pop", "LT": "Tropical", "LB": "Brazilian Funk", "RK": "Rock", "RM": "Metal",
    "RA": "Alternative", "CO": "Country"}
STYLE_WORDS = [(r"\bjersey\b|\bphilly club\b", "JC"), (r"\bamapiano\b|\bpiano remix\b", "AP"),
    (r"\bbaile\b|\bbatida\b|\bmontagem\b|\bfunk brasil|\bbrazilian funk\b", "LB"), (r"\bkompa\b|\bkonpa\b", "CK"),
    (r"\bsoca\b", "CS"), (r"\bzess\b", "CZ"), (r"\bdancehall\b", "CD"), (r"\breggaeton\b|\bdembow\b|\bmoombah", "LR"),
    (r"\bbachata\b|\bsalsa\b|\bmerengue\b", "LT"), (r"\bdubstep\b", "DB"), (r"\bdrill\b", "HD"),
    (r"\bafro house\b", "UA"), (r"\btech house\b", "UT"),
    (r"\bhouse (remix|edit|mix|flip|bootleg)\b", "UC"), (r"\bnola bounce\b", "HB")]
TAG_WORDS = [(r"jersey", "JC"), (r"amapiano", "AP"), (r"kompa|konpa", "CK"), (r"zess", "CZ"), (r"soca", "CS"),
    (r"dancehall|reggae", "CD"), (r"reggaeton|latin|dembow", "LR"), (r"afro", "AA"), (r"drill", "HD"),
    (r"house", "DH"), (r"dubstep|bass music", "DB"), (r"country", "CO"), (r"r&b|rnb|soul", "RB"),
    (r"hip.?hop|rap|trap", "HR"), (r"edm|electronic|dance", "DE"), (r"rock|alternative|indie", "RA"), (r"pop", "PP")]
_ARTISTS = None


def artist_genres():
    global _ARTISTS
    if _ARTISTS is None:
        try:
            _ARTISTS = json.loads((Path(__file__).resolve().parent / "artist_genres.json").read_text())
        except Exception:
            _ARTISTS = {}
    return _ARTISTS


def primary_artist(a):
    return re.split(r"\s+(?:ft\.?|feat\.?|featuring|x|&|and|with|vs\.?)\s+|,|;|/", a, flags=re.I)[0].strip().lower()


_SONGS = None


def song_genres():
    global _SONGS
    if _SONGS is None:
        try:
            _SONGS = json.loads((Path(__file__).resolve().parent / "song_genres.json").read_text())
        except Exception:
            _SONGS = {}
    return _SONGS


def pick_genre(artist, title, info):
    """Return a genre code, or None when unsure."""
    songs = song_genres()
    for a in [artist] + re.split(r"\s*(?:,|&|\bx\b|\bfeat\.?|\bft\.?|\band\b)\s*", artist, flags=re.I):
        code = songs.get(song_key(a, title)) if a.strip() else None
        if code in GENRE_PATH:
            return code
    text = f"{title} {info.get('title') or ''}".lower()
    for pat, code in STYLE_WORDS:
        if re.search(pat, text):
            return code
    known = artist_genres()
    for a in [artist] + re.split(r"\s+(?:ft\.?|feat\.?|featuring|x|&|and|with)\s+|,", artist, flags=re.I)[1:]:
        code = known.get(primary_artist(a))
        if code in GENRE_PATH:
            return code
    learned = _learned()
    if learned.get(primary_artist(artist)) in GENRE_PATH:
        return learned[primary_artist(artist)]
    tags = " ".join([info.get("genre") or ""] + list(info.get("genres") or []) + list(info.get("tags") or [])).lower()
    for pat, code in TAG_WORDS:
        if re.search(pat, tags):
            return code
    return None


# --------------------------------------------------------------------------- genre lookup online
LEARNED = APP_SUPPORT / "learned_genres.json"
DEEZER_GENRES = [("rap", "HR"), ("hip hop", "HR"), ("r&b", "RB"), ("soul", "RF"), ("funk", "RF"), ("disco", "RF"),
    ("reggaeton", "LR"), ("latin", "LR"), ("brazil", "LB"), ("reggae", "CD"), ("dancehall", "CD"), ("african", "AA"),
    ("afro", "AA"), ("k-pop", "PK"), ("asian", "PK"), ("metal", "RM"), ("alternative", "RA"), ("indie", "RA"),
    ("rock", "RK"), ("country", "CO"), ("electro", "DE"), ("dance", "DE"), ("house", "UC"), ("jazz", "RC"),
    ("pop", "PP")]


def _learned():
    try:
        return json.loads(LEARNED.read_text())
    except Exception:
        return {}


def _remember(artist, code):
    d = _learned()
    d[primary_artist(artist)] = code
    try:
        tmp = LEARNED.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(LEARNED)
    except Exception:
        pass


_HOST_LAST = {}
_HOST_GAP = {"musicbrainz.org": 1.1, "api.deezer.com": 0.25}


def _get_json(url):
    """GET a JSON API, spaced out per site (MusicBrainz allows 1 a second, Deezer about 50 per 5 s) and
    tried again after a pause when the site says slow down or has a hiccup, instead of giving up."""
    import urllib.request, urllib.error
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    last = None
    for attempt in range(4):
        gap = _HOST_GAP.get(host, 0.0)
        time.sleep(max(0.0, gap - (time.time() - _HOST_LAST.get(host, 0.0))))
        _HOST_LAST[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "DJGrab/1.0 (personal DJ library tool)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("error"), dict) and data["error"].get("code") == 4:
                raise urllib.error.URLError("rate limited")      # Deezer's "quota exceeded"
            return data
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            last = ex
            if isinstance(ex, urllib.error.HTTPError) and ex.code in (400, 404):
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last


def _by_era(code, year):
    if not year:
        return code
    if code == "PP" and year < 2005:
        return "PC"
    if code == "RB" and year < 2000:
        return "RC"
    if code == "HR" and year < 2000:
        return "HO"
    return code


def lookup_genre(artist, title):
    """Ask Deezer (then MusicBrainz) what genre a song is. Returns a code or None."""
    import urllib.parse
    a, t = primary_artist(artist), re.sub(r"\s*[\(\[].*$", "", title).strip()
    try:
        q = urllib.parse.quote(f'artist:"{a}" track:"{t}"')
        hits = _get_json(f"https://api.deezer.com/search?q={q}&limit=3").get("data") or []
        if not hits:
            hits = _get_json("https://api.deezer.com/search?limit=3&q=" + urllib.parse.quote(f"{a} {t}")).get("data") or []
        for h in hits:
            alb = (h.get("album") or {}).get("id")
            if not alb:
                continue
            album = _get_json(f"https://api.deezer.com/album/{alb}")
            names = " / ".join(g.get("name", "") for g in (album.get("genres") or {}).get("data", [])).lower()
            year = int((album.get("release_date") or "0")[:4] or 0)
            for word, code in DEEZER_GENRES:
                if word in names:
                    return _by_era(code, year)
    except Exception:
        pass
    try:
        q = urllib.parse.quote(f'artist:"{a}"')
        arts = _get_json(f"https://musicbrainz.org/ws/2/artist?query={q}&fmt=json&limit=1").get("artists") or []
        tags = " ".join(x.get("name", "") for x in (arts[0].get("tags") or [])) if arts else ""
        for pat, code in TAG_WORDS:
            if re.search(pat, tags.lower()):
                return code
    except Exception:
        pass
    return None


# Genres with no folder yet get a new one (owner's library only). Which top folder a new style goes under:
NEW_GENRE_TOPS = [(r"bachata|merengue|salsa|cumbia|corrido|banda|norte|mariachi|regional mexican|latin|"
                   r"tango|bolero|vallenato|dembow|trap latino", "Latin"),
                  (r"trap|rap|hip ?hop|grime|phonk|boom bap", "Hip Hop"),
                  (r"r&b|rnb|soul|neo soul|motown", "R&B"),
                  (r"house|techno|trance|garage|drum and bass|dnb|edm|electro|dubstep|hardstyle", "Dance & Electronic"),
                  (r"afro|highlife|gqom|kwaito|bongo", "African"),
                  (r"reggae|dancehall|soca|calypso|zouk|kompa", "Caribbean"),
                  (r"rock|metal|punk|grunge|emo|indie", "Rock"),
                  (r"country|bluegrass|americana", "Country"),
                  (r"pop", "Pop")]
NEW_GENRE_SKIP = {"music", "pop", "rap", "hip hop", "dance", "electronic", "latin music", "latin", "alternative",
                  "rock", "r&b", "films/games", "kids", "other", "seen live", "american", "british", "female vocalists",
                  "male vocalists", "singer-songwriter", "usa", "uk", "favorites", "favourites"}


# Shared app sorting: broad folders, and in Detailed mode a sub-genre folder once it would hold a few songs
SHARED_TOP = {"Hip Hop": "Hip Hop", "R&B": "R&B", "Pop": "Pop", "House": "Dance & House",
              "Dance & Electronic": "Dance & House", "Latin": "Latin", "African": "Afro & Caribbean",
              "Caribbean": "Afro & Caribbean", "Country": "Country", "Rock": "Rock"}
SORT_MAP = APP_SUPPORT / "sort_map.json"      # their own changes: {"Drill": "Hip Hop"} keeps Drill in Hip Hop
SUB_MIN = 3                                    # a sub-genre gets its own folder from its 3rd song on


def shared_folder(code, mode, root):
    """Where a song goes in the shared app: "Hip Hop" (Simple) or "Hip Hop/Drill" (Detailed). Songs whose
    genre isn't known stay in the main folder. A sub-genre folder is only made once it would hold
    SUB_MIN songs; until then they sit in the main genre folder, and when it opens, DJ Grab's own earlier
    songs of that sub-genre move into it (only files DJ Grab made, found by its tag)."""
    if not code or code not in GENRE_PATH:
        return None
    top_lib, sub = GENRE_PATH[code].split("/", 1)
    top = SHARED_TOP.get(top_lib, top_lib)
    try:
        user = json.loads(SORT_MAP.read_text())
    except Exception:
        user = {}
    if sub in user:                       # e.g. "Drill": "Hip Hop" → no Drill folder
        return user[sub] or None
    if top_lib in user:
        top = user[top_lib] or top
    if mode != "detailed" or sub.lower() == top_lib.lower():
        return top
    subdir = root / top / sub
    if subdir.is_dir():
        return f"{top}/{sub}"
    # count DJ Grab's songs of this sub-genre waiting in the main folder
    mine = []
    for f in (root / top).glob("*.mp3") if (root / top).is_dir() else []:
        try:
            from mutagen.id3 import ID3
            if str(ID3(str(f)).get("TXXX:DJ Grab genre", "")) == code:
                mine.append(f)
        except Exception:
            continue
    if len(mine) + 1 >= SUB_MIN:
        subdir.mkdir(parents=True, exist_ok=True)
        for f in mine:
            try:
                os.replace(f, subdir / f.name)
            except Exception:
                pass
        return f"{top}/{sub}"
    return top


def new_genre_folder(artist, title):
    """A song the known genres don't cover (gospel, bachata, jazz...): the music catalog's most specific
    genre for it, as a folder path like "Latin/Bachata" or "Gospel/Gospel". None when nothing clear."""
    import urllib.parse
    a, t = primary_artist(artist), re.sub(r"\s*[\(\[].*$", "", title).strip()
    names = []
    try:   # MusicBrainz tags on the recording, then the artist: specific styles, most-voted first
        q = urllib.parse.quote(f'recording:"{t}" AND artist:"{a}"')
        recs = _get_json(f"https://musicbrainz.org/ws/2/recording?query={q}&fmt=json&limit=3").get("recordings") or []
        for r in recs[:1]:
            names += [x["name"] for x in sorted(r.get("tags") or [], key=lambda x: -x.get("count", 0))]
        q = urllib.parse.quote(f'artist:"{a}"')
        arts = _get_json(f"https://musicbrainz.org/ws/2/artist?query={q}&fmt=json&limit=1").get("artists") or []
        if arts:
            names += [x["name"] for x in sorted(arts[0].get("tags") or [], key=lambda x: -x.get("count", 0))]
    except Exception:
        pass
    try:   # Deezer's album genres as the backup
        q = urllib.parse.quote(f'artist:"{a}" track:"{t}"')
        hits = _get_json(f"https://api.deezer.com/search?q={q}&limit=1").get("data") or []
        if hits and (hits[0].get("album") or {}).get("id"):
            album = _get_json(f"https://api.deezer.com/album/{hits[0]['album']['id']}")
            names += [g.get("name", "") for g in (album.get("genres") or {}).get("data", [])]
    except Exception:
        pass
    for n in names:
        n = re.sub(r"\s+", " ", (n or "").strip())
        if not n or n.lower() in NEW_GENRE_SKIP or len(n) > 30 or re.search(r"\d{4}|[/\\:]", n):
            continue
        name = " ".join(w if w.isupper() else w.capitalize() for w in n.split())
        top = next((tp for pat, tp in NEW_GENRE_TOPS if re.search(pat, n.lower())), name)
        return f"{top}/{name}"
    return None


DURATIONS = APP_SUPPORT / "durations.json"
BPMS = APP_SUPPORT / "official_bpm.json"


def official_bpm(artist, title):
    """The official tempo saved by lookup_duration (Deezer), or None."""
    if not artist or not title:
        return None
    lookup_duration(artist, title)
    try:
        return json.loads(BPMS.read_text()).get(f"{artist.lower()}|{title.lower()}")
    except Exception:
        return None
EDIT_WORDS = re.compile(r"\b(remix|rmx|edit|extended|mix|intro|version|live|bootleg|flip|refix|redrum|vip|sped|slowed|"
                        r"acapella|acap|instrumental|mashup|cover|jersey|club)\b", re.I)




def lookup_duration(artist, title):
    """Length in seconds of the official (album) version of a song, or None. Deezer first, then MusicBrainz.
    None for remixes, edits and other versions, whose length legitimately differs. Cached."""
    import urllib.parse
    if not artist or not title or EDIT_WORDS.search(title):
        return None
    key = f"{artist.lower()}|{title.lower()}"
    try:
        cache = json.loads(DURATIONS.read_text())
    except Exception:
        cache = {}
    if key in cache and not _retry(cache[key]):
        return cache[key] if not isinstance(cache[key], dict) else None
    a = primary_artist(artist)
    t = re.sub(r"\s*[\(\[].*$", "", title).strip()
    norm = lambda x: re.sub(r"[^a-z0-9]+", "", re.sub(r"\s*[\(\[].*$", "", fold(x)))
    found = None
    try:
        q = urllib.parse.quote(f'artist:"{a}" track:"{t}"')
        hits = _get_json(f"https://api.deezer.com/search?q={q}&limit=10").get("data") or []
        good = [h for h in hits if norm(h.get("title")) == norm(t) and h.get("duration")
                and not EDIT_WORDS.search(h.get("title_version") or "") and not EDIT_WORDS.search(h.get("title") or "")]
        lens = [h.get("duration") for h in good]
        if lens:
            found = float(sorted(lens)[len(lens) // 2])
        for h in good[:3]:                              # the official tempo, when Deezer has one
            try:
                b = float(_get_json(f"https://api.deezer.com/track/{h['id']}").get("bpm") or 0)
            except Exception:
                b = 0
            if b > 40:
                bpms = json.loads(BPMS.read_text()) if BPMS.exists() else {}
                bpms[key] = b
                tmp = BPMS.with_suffix(".tmp"); tmp.write_text(json.dumps(bpms)); tmp.replace(BPMS)
                break
    except Exception:
        pass
    if found is None:
        try:
            q = urllib.parse.quote(f'recording:"{t}" AND artist:"{a}"')
            recs = _get_json(f"https://musicbrainz.org/ws/2/recording?query={q}&fmt=json&limit=10").get("recordings") or []
            lens = [r["length"] / 1000 for r in recs if r.get("length") and norm(r.get("title")) == norm(t)]
            if lens:
                found = float(sorted(lens)[len(lens) // 2])
        except Exception:
            found = None
    cache[key] = found if found is not None else {"none": time.time()}
    try:
        tmp = DURATIONS.with_suffix(".tmp"); tmp.write_text(json.dumps(cache)); tmp.replace(DURATIONS)
    except Exception:
        pass
    return found


YEARS = APP_SUPPORT / "years3.json"     # v3: stricter artist match, and failed lookups are no longer saved as "none"
MOMENTS = Path(__file__).resolve().parent / "moments.json"      # moment / crowd tags Claude confirmed online
AUTO_RENAMES = APP_SUPPORT / "auto_renames.json"                # (Acap In) / (Acap Out) labels the app found


def _retry(v):
    """A lookup that found nothing is tried again after 3 days (and old-style saved misses right away)."""
    if v is None:
        return True
    return isinstance(v, dict) and time.time() - float(v.get("none", 0)) > 3 * 86400


_MB_LAST = 0.0


def lookup_year(artist, title):
    """Year the song first came out, or None. MusicBrainz's first-release date for the recording (a
    compilation or reissue doesn't make it newer), Deezer's earliest album date as the backup. Versions
    (remix, edit, intro) count as the song they're a version of. Cached."""
    import urllib.parse
    if not artist or not title:
        return None
    a = primary_artist(artist)
    t = re.sub(r"\s*[\(\[].*$", "", title).strip()
    t = re.sub(r"\s+-\s+.*$", "", t).strip() or t
    key = f"{a.lower()}|{t.lower()}"
    try:
        cache = json.loads(YEARS.read_text())
    except Exception:
        cache = {}
    if key in cache and not _retry(cache[key]):
        return cache[key] if not isinstance(cache[key], dict) else None
    norm = lambda x: re.sub(r"[^a-z0-9]+", "", re.sub(r"\s*[\(\[].*$", "", fold(x)))
    na = re.sub(r"[^a-z0-9]+", "", fold(a))
    years, failed = [], False
    global _MB_LAST
    try:
        time.sleep(max(0.0, 1.1 - (time.time() - _MB_LAST)))     # MusicBrainz allows one request a second
        _MB_LAST = time.time()
        q = urllib.parse.quote(f'recording:"{t}" AND artist:"{a}"')
        recs = _get_json(f"https://musicbrainz.org/ws/2/recording?query={q}&fmt=json&limit=25").get("recordings") or []
        for r in recs:
            # the artist has to be one of the credited names, not just letters inside one ("LISA" is not
            # "Lisa Stansfield")
            names = {re.sub(r"[^a-z0-9]+", "", fold(c.get("name", ""))) for c in r.get("artist-credit") or []}
            names |= {re.sub(r"[^a-z0-9]+", "", fold((c.get("artist") or {}).get("name", ""))) for c in r.get("artist-credit") or []}
            d = (r.get("first-release-date") or "")[:4]
            if norm(r.get("title")) == norm(t) and na and na in names and d.isdigit() and int(d) > 1900:
                years.append(int(d))
    except Exception:
        failed = True
    if not years:
        try:
            q = urllib.parse.quote(f'artist:"{a}" track:"{t}"')
            hits = _get_json(f"https://api.deezer.com/search?q={q}&limit=10").get("data") or []
            same = lambda h: re.sub(r"[^a-z0-9]+", "", fold((h.get("artist") or {}).get("name", ""))) == na
            for h in [h for h in hits if norm(h.get("title")) == norm(t) and same(h)][:4]:
                al = _get_json(f"https://api.deezer.com/album/{h['album']['id']}")
                d = (al.get("release_date") or "")[:4]
                if d.isdigit() and int(d) > 1900:
                    years.append(int(d))
        except Exception:
            failed = True
    found = min(years) if years else None
    if found is None and failed:
        return None                                # couldn't reach the sites: ask again next time
    cache[key] = found if found is not None else {"none": time.time()}
    try:
        tmp = YEARS.with_suffix(".tmp"); tmp.write_text(json.dumps(cache)); tmp.replace(YEARS)
    except Exception:
        pass
    return found


def era_label(year):
    if not year:
        return None
    y = int(year)
    return f"{y // 10 % 10}0s" if y < 2000 else f"{y // 10 * 10}s"


def song_comment(artist, title, file_year=None):
    """What goes in Serato's Comment column: the song's era, then up to two moment / crowd tags
    (Wedding, Line dance...) when they've been confirmed. Searchable, e.g. "90s · Line dance"."""
    try:
        m = json.loads(MOMENTS.read_text()).get(song_key(artist, title), [])
    except Exception:
        m = []
    moments = m.get("tags", []) if isinstance(m, dict) else m
    year = (m.get("year") if isinstance(m, dict) else None) or lookup_year(artist, title)   # a year Claude confirmed wins
    if not year and file_year:
        try:
            y = int(str(file_year)[:4])
            year = y if 1900 < y <= time.localtime().tm_year else None
        except Exception:
            year = None
    parts = [p for p in [era_label(year)] + list(moments)[:2] if p]
    return ", ".join(parts)


_HEX = re.compile(r"^\s*([0-9A-Fa-f]{8}\s*){4,}$")


def _unmerge_comment(t, cur):
    """Undo an earlier DJ Grab bug that joined all of a file's comment frames into the visible one with
    " | " (iTunes' hidden gain and gapless data, or the same comment twice). The hidden iTunes values go
    back where they were; a part repeated word for word is kept once. Nothing you wrote is dropped."""
    from mutagen.id3 import COMM
    tail = ""
    ours = next((str(x) for x in t.getall("TXXX:DJ Grab comment")), "")
    body = cur
    if ours and body.endswith(ours):
        body, tail = body[:len(body) - len(ours)].rstrip(" ·"), ours
    parts = [x.strip() for x in body.split(" | ")]
    hexes = [x for x in parts if _HEX.match(x)]
    keep = []
    for x in parts:
        if _HEX.match(x) or (hexes and re.fullmatch(r"[01]", x)) or x in keep:
            continue
        keep.append(x)
    have = {c.desc for c in t.getall("COMM")}
    for x in hexes:
        desc = "iTunSMPB" if x.split()[0] == "00000000" else "iTunNORM"
        if desc not in have:
            t.add(COMM(encoding=3, lang="eng", desc=desc, text=" " + x))
            have.add(desc)
    if hexes and "iTunPGAP" not in have and any(re.fullmatch(r"[01]", x) for x in parts):
        t.add(COMM(encoding=3, lang="eng", desc="iTunPGAP", text=next(x for x in parts if re.fullmatch(r"[01]", x))))
    body = " | ".join(x for x in keep if x)
    return " · ".join(x for x in (body, tail) if x)


def set_comment(t, text):
    """Add the attributes to Serato's Comment column without ever clearing what's there: your comment (or a
    DJ pool's) stays first and DJ Grab's part goes after it ("R&B, Salsa · 2010s"). Only DJ Grab's own part
    is ever updated; it's remembered in a hidden tag. A comment an earlier version moved aside is put back."""
    from mutagen.id3 import COMM, TXXX
    # only the plain comment (the one Serato shows) is read and written; iTunes' hidden ones (iTunNORM,
    # iTunSMPB...) and other apps' named comments are left alone
    mine = [c for c in t.getall("COMM") if not c.desc]
    cur = shown = " | ".join(str(c) for c in mine if str(c).strip())
    ours0 = next((str(x) for x in t.getall("TXXX:DJ Grab comment")), None)
    if ours0 is not None and " | " in cur:
        cur = _unmerge_comment(t, cur)
    ours = next((str(x) for x in t.getall("TXXX:DJ Grab comment")), None)
    backup = next((str(x) for x in t.getall("TXXX:DJ Grab old comment")), None)
    if backup:
        base = backup                              # an earlier version moved your comment aside
    elif ours and cur.endswith(ours):
        base = cur[:len(cur) - len(ours)].rstrip(" ·,")
    elif ours is None and re.match(r"^(\d0s|\d{4}s)( · [^|]*)?$", cur):
        base = ""                                  # an older DJ Grab comment with no note: it was ours
    else:
        base = cur
    if not text and ours:
        text = ours                                # nothing found this time: keep what was there
    # everything in the comment separated by a comma ("KPop, Mainstream, 2010s, Wedding"), each thing once
    split_ = lambda x: [q for q in re.split(r"\s*(?:,|·|\|)\s*", x or "") if q]
    tparts = split_(text)
    text = ", ".join(tparts)
    bparts, links = [], []
    for q in split_(base):
        if _JUNK_COMMENT.search(q):
            links.append(q)                        # no links or download-site text in the comment
        elif q not in bparts and q not in tparts:
            bparts.append(q)
    if links:
        prev = next((str(v) for v in t.getall("TXXX:DJ Grab removed comment")), "")
        if not all(q in prev for q in links):
            t.setall("TXXX:DJ Grab removed comment", [TXXX(encoding=3, desc="DJ Grab removed comment",
                                                           text=" | ".join(q for q in [prev] + links if q))])
    base = ", ".join(bparts)
    new = ", ".join(x for x in (base, text) if x)
    t.delall("TXXX:DJ Grab old comment")
    if new == shown and (ours == text or not text):
        return bool(backup)
    for c in mine:
        t.delall(c.HashKey)
    t.add(COMM(encoding=3, lang="eng", desc="", text=new))
    if text:
        t.setall("TXXX:DJ Grab comment", [TXXX(encoding=3, desc="DJ Grab comment", text=text)])
    return True


def library_root(outdir):
    """The folder holding the genre folders, if the save folder sits next to them."""
    parent = Path(outdir).parent
    if (parent / "Hip Hop").is_dir() and (parent / "Pop").is_dir():
        return parent
    return None


def find_in_library(root, base):
    """Already have this track anywhere in the genre folders?"""
    for ext in (".mp3", ".aiff", ".aif", ".m4a", ".wav"):
        for code, rel in GENRE_PATH.items():
            p = root / rel / f"{base}{ext}"
            if p.exists():
                return p
    return None


# --------------------------------------------------------------------------- "do I already have this?"

_INDEX = None


def fold(x):
    """Lowercase with accents taken off (Río = Rio, Maniquí = Maniqui), so names match however they're spelled."""
    import unicodedata as _u
    return "".join(c for c in _u.normalize("NFKD", x or "") if not _u.combining(c)).lower()


def song_key(artist, title):
    """Artist + core title, ignoring versions (Clean, Remix, Intro...), features and punctuation."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", fold(title))
    t = re.sub(r"\b(feat|ft)\.?\s.*$", " ", t)
    t = re.sub(r"[^a-z0-9]+", "", t)
    a = re.sub(r"[^a-z0-9]+", "", fold(primary_artist(artist))) if artist else ""
    return f"{a}|{t}"


def library_index(root):
    """Map song_key -> files, for every audio file under the library folder."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(("_", "."))]   # _To Delete, _Exact copies... don't count
        for f in fn:
            stem, ext = os.path.splitext(f)
            if f.startswith(".") or ext.lower() not in (".mp3", ".aiff", ".aif", ".m4a", ".wav", ".mp4", ".flac"):
                continue
            stem = re.sub(r" \(\d+\)$", "", stem)
            a, _, t = stem.partition(" - ")
            # files with an artist match on artist + title; files without one match on title alone
            k = song_key(a, t) if t else song_key("", a)
            idx.setdefault(k, []).append(os.path.relpath(os.path.join(dp, f), root))
    _INDEX = idx
    return idx


_SAME_AS_ORIGINAL = re.compile(r"^(clean|dirty|explicit|super clean|radio( edit| version)?|single( version)?|"
                               r"original( mix| version)?|album version|feat\.?.*|ft\.?.*|with .*|official.*|lyrics?.*|"
                               r"audio.*|hq|hd|remaster(ed)?.*|\d+|\d{4}|prod\.?.*)$")


def version_sig(title):
    """What makes this a different version: remix, edit, intro, instrumental... (not clean/dirty/feat)."""
    parts = re.findall(r"[\(\[]([^\)\]]+)[\)\]]", title)
    m = re.search(r"\s-\s+([^-]*(remix|edit|mix|refix|flip|bootleg|vip|rework|dub|intro|version))\s*$", title, re.I)
    if m:
        parts.append(m.group(1))
    sig = set()
    for p in parts:
        q = re.sub(r"\b(super )?(clean|dirty|explicit)\b|\b\d{4}\s+(remaster(ed)?|version)\b", " ", p.lower())
        q = re.sub(r"\s+", " ", q).strip(" -")
        if not q or _SAME_AS_ORIGINAL.match(q):
            continue
        sig.add(re.sub(r"[^a-z0-9]+", "", q))
    return frozenset(sig)


def file_title(rel):
    stem = re.sub(r" \(\d+\)$", "", os.path.splitext(os.path.basename(rel))[0])
    return stem.split(" - ", 1)[1] if " - " in stem else stem


def library_matches(root, artist, title):
    if not root:
        return []
    idx = library_index(root)
    found = list(dict.fromkeys(idx.get(song_key(artist, title), []) + idx.get(song_key("", title), [])))
    return found[:6]


# --------------------------------------------------------------------------- search by song name

PREFERRED_CHANNELS = ["vibe music", "7clouds"]       # lyric channels with clean album audio
BAD_VERSION = re.compile(r"\b(extended|music video|official video|official music video|video oficial|live|concert|"
                         r"cover|karaoke|sped ?up|slowed|reverb|nightcore|8d|1 hour|10 hours|loop|"
                         r"instrumental|acapella|remix|mashup|reaction|tiktok|bass ?boosted|teaser|snippet|"
                         r"remake|recreation|recreated|logic pro|fl studio|ableton|tutorial|how to|type beat|piano version)\b", re.I)


def _words(s):
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if len(w) > 1 or w.isdigit()]


def score_candidate(q_artist, q_title, cand, typical_len=None):
    """Higher is better. None = not acceptable."""
    title = cand.get("title") or ""
    chan = (cand.get("channel") or cand.get("uploader") or "").lower()
    dur = cand.get("duration") or 0
    tl = title.lower()
    # the song title must be in the video title
    if not all(w in tl for w in _words(q_title)):
        return None
    # versions the user didn't ask for
    for m in BAD_VERSION.finditer(tl):
        if m.group(0).lower() not in (q_title + " " + q_artist).lower():
            return None
    if dur and (dur < 75 or dur > 480):
        return None
    if q_artist:
        aw = _words(primary_artist(q_artist).replace("!", "i"))
        if aw and not all(w in (tl + " " + chan).replace("!", "i") for w in aw):
            return None                                # a cover band or wrong song
        if re.search(r"\b(studio crew|tribute|made famous|in the style of|originally performed)\b", tl + " " + chan):
            return None
        if re.search(r"\bby\s+" + re.escape(primary_artist(q_artist)), tl):
            return None                                # "Song by Artist" = somebody else's cover
    if LIVE_RE.search(tl) or re.search(r"\s\d{5,6}\b", tl):
        return None                                    # festival / fan recordings
    if LIVE_CHANNELS.search(chan) and not re.search(r"\blive\b", (q_title + " " + q_artist).lower()):
        return None                                    # a live-session channel (KEXP, NPR...): not the record
    sc = 0
    if any(c in chan for c in PREFERRED_CHANNELS):
        sc += 100
    elif chan.endswith(" - topic"):
        sc += 80                                       # official audio from YouTube Music
    elif "official audio" in tl or "audio" in tl:
        sc += 50
    elif "lyric" in tl:
        sc += 35
    if q_artist and all(w in (tl + " " + chan) for w in _words(primary_artist(q_artist))):
        sc += 30
    if typical_len and dur:
        sc -= min(60, abs(dur - typical_len) / 2)     # longer than the usual cut = intro/skit/extended
    return sc


def friendly_error(ex):
    """Plain-language reasons for the common failures, instead of the raw technical message."""
    m = str(ex)
    low = m.lower()
    rules = [
        (("getaddrinfo", "name resolution", "nodename nor servname", "network is unreachable", "no route to host",
          "failed to resolve", "connection refused", "urlopen error", "unable to connect", "network is down",
          "internet connection appears to be offline"),
         "No internet connection. Connect to Wi-Fi and try again."),
        (("timed out", "timeout", "read operation timed out"),
         "The connection is too slow or dropped. Check your Wi-Fi and try again."),
        (("http error 429", "too many requests"),
         "YouTube is limiting downloads right now. Wait a few minutes, then retry."),
        (("sign in to confirm", "confirm you're not a bot", "confirm you are not a bot"),
         "YouTube wants to check you're not a bot. Turn on Browser sign-in in Settings, or try again later."),
        (("private video",), "That video is private. Try a different link."),
        (("video unavailable", "has been removed", "no longer available", "account associated with this video has been terminated"),
         "That video was removed or isn't available. Try a different link."),
        (("not available in your country", "geo restrict", "blocked it in your country"),
         "That video is blocked in your country. Try a different upload."),
        (("age-restricted", "age restricted", "inappropriate for some users"),
         "That video is age-restricted. Turn on Browser sign-in in Settings to get it."),
        (("no space left on device",), "The drive is full. Free up some space and try again."),
        (("permission denied", "operation not permitted", "read-only file system"),
         "Can't save to that folder. Pick a different folder in Settings."),
        (("isn't plugged in",), m),
        (("ffmpeg", "ffprobe"), "The audio converter isn't working. Reopen DJ Grab to repair it."),
        (("unsupported url",), "That link isn't a song or video page DJ Grab can read."),
    ]
    for keys, msg in rules:
        if any(k in low for k in keys):
            return msg
    return m[:300]


def why_not_found(a, t, cands):
    """A clear reason when no upload could be used, worked out from what YouTube did return."""
    name = f'"{t}"' + (f" by {a}" if a else "")
    if not cands:
        return f"Couldn't find {name} anywhere. Check the spelling, or add the artist."
    tl = [(c.get("title") or "").lower() for c in cands]
    has_title = [x for x in tl if all(w in x for w in _words(t))]
    if not has_title:
        return f"Couldn't find a song called {name}. Check the spelling" + ("" if a else ", or add the artist") + "."
    if a:
        aw = _words(primary_artist(a))
        by_artist = [x for x, c in zip(tl, cands) if all(w in x + " " + (c.get("channel") or "").lower() for w in aw)
                     and all(w in x for w in _words(t))]
        if not by_artist:
            return f"Found {t}, but only by other artists or cover versions, not by {a}. Check the artist name."
    else:
        by_artist = has_title
    reasons = []
    if any(LIVE_RE.search(x) for x in by_artist):
        reasons.append("live")
    if any(BAD_VERSION.search(x) for x in by_artist):
        found = {m.group(0).lower() for x in by_artist for m in BAD_VERSION.finditer(x)}
        reasons.append(" / ".join(sorted(found))[:60])
    long_ = [c for c in cands if (c.get("duration") or 0) > 480]
    if long_:
        reasons.append("over 8 minutes long")
    if reasons:
        return f"Only found versions of {name} that aren't the normal song ({', '.join(reasons)}). Paste a YouTube link to the one you want."
    return f"Found {name}, but no upload passed the quality checks. Paste a YouTube link to the one you want."


def search_song(query):
    """Find the best YouTube upload for 'Artist - Title'. Returns (url, label) or (None, reason)."""
    import yt_dlp
    a, t = split_query(query)
    a, t, _ = correct_query(a, t)
    query = f"{a} {t}".strip()
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as y:
        res = y.extract_info(f"ytsearch15:{query} lyrics", download=False)
        res2 = y.extract_info(f"ytsearch10:{query} audio", download=False)
    cands, seen = [], set()
    for e in (res.get("entries") or []) + (res2.get("entries") or []):
        if e and e.get("id") not in seen:
            seen.add(e.get("id")); cands.append(e)
    ok = [c for c in cands if score_candidate(a, t, c) is not None]
    if not ok:
        return [], why_not_found(a, t, cands)
    lens = sorted(c.get("duration") or 0 for c in ok if c.get("duration"))
    typical = lens[len(lens) // 2] if lens else None
    ranked = sorted(ok, key=lambda c: score_candidate(a, t, c, typical), reverse=True)[:3]
    return [(f"https://www.youtube.com/watch?v={c['id']}",
             f"{c.get('title')} ({c.get('channel') or c.get('uploader') or ''})") for c in ranked], None


def _clean_query(q):
    q = re.sub(r"\*\(.*?\)\*|\*\*|\*|_", " ", q)          # notes like *(great for the entrance)*
    q = re.sub(r"^\s*(?:[-•]|\d+[.)])\s*", "", q)              # list bullets and numbers
    q = q.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", q).strip()


def _deezer_hit(artist, title):
    """True if Deezer knows a song with this artist and title (used to tell 'Title - Artist' from
    'Artist - Title')."""
    import urllib.parse
    try:
        q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"' if artist else f'track:"{title}"')
        return bool(_get_json(f"https://api.deezer.com/search?q={q}&limit=1").get("data"))
    except Exception:
        return False


def _norm(x):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", x.lower())).strip()


def _best_match(a, t, cands):
    """The candidate (artist, title) that looks most like what was typed, and how alike (0-1)."""
    import difflib
    sim = lambda x, y: difflib.SequenceMatcher(None, _norm(x), _norm(y)).ratio()
    best, score = None, 0.0
    for ha, ht in cands:
        if not ht:
            continue
        # a catalog entry for a live / remix / other version is not the song you typed
        extra_words = BAD_VERSION.findall(ht.lower()) + LIVE_RE.findall(ht.lower())
        if any(w.lower() not in (a + " " + t).lower() for w in extra_words):
            continue
        ht = re.sub(r"\s*\((?:feat|ft|with)\.?[^)]*\)", "", ht, flags=re.I).strip()
        if a:   # artist and title both given: each must look alike
            sc = min(sim(a, ha.split(" feat")[0]) if " feat" in ha.lower() else sim(a, ha), sim(t, ht))
            sc = max(sc, min(sim(a, ha), sim(t, ht)))
        else:   # one run of words: the title alone, or the artist and title together
            sc = max(sim(t, ht), sim(t, f"{ha} {ht}"), sim(t, f"{ht} {ha}"))
        if sc > score:
            best, score = (ha, ht), sc
    return best, score


CATALOG_NAME = None     # (artist, title) as the music catalog spells them, from the last search


def correct_query(a, t):
    """Fix small typos in a typed song name ("Levitatin by Dua Lipaa") and a backwards order ("War - Low
    Rider" typed as Artist - Title) using the music catalogs: their best match is taken when it is clearly
    the same song. Both orders are tried. Returns (artist, title, fixed)."""
    import urllib.parse
    typed = f"{a} {t}".strip()
    if not typed:
        return a, t, False
    try:
        hits = _get_json("https://api.deezer.com/search?limit=8&q=" + urllib.parse.quote(typed)).get("data") or []
    except Exception:
        hits = []
    cands = [((h.get("artist") or {}).get("name", ""), h.get("title_short") or h.get("title", "")) for h in hits]
    orders = [(a, t)] + ([(t, a)] if a else [])
    best, score, used = None, 0.0, (a, t)
    for oa, ot in orders:
        b, sc = _best_match(oa, ot, cands)
        if b and sc > score:
            best, score, used = b, sc, (oa, ot)
    if not best or score < 0.8:
        # the catalog's search wants the words spelled right; MusicBrainz can look up words that are
        # close (one or two letters off), so "uptwon funk" by "mark ronsen" still finds Uptown Funk
        words = lambda x: " ".join(w + "~" for w in re.findall(r"[a-z0-9']{2,}", _norm(x)))
        for oa, ot in orders:
            mq = (f"recording:({words(ot)}) AND artist:({words(oa)})" if oa
                  else f"recording:({words(ot)}) OR artist:({words(ot)})")
            try:
                rec = _get_json("https://musicbrainz.org/ws/2/recording?fmt=json&limit=15&query="
                                + urllib.parse.quote(mq)).get("recordings") or []
            except Exception:
                rec = []
            mb = [("".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in r.get("artist-credit", [])).strip(),
                   r.get("title", "")) for r in rec]
            b2, s2 = _best_match(oa, ot, mb)
            if b2 and s2 > score:
                best, score, used = b2, s2, (oa, ot)
    # a fuzzy match has to be close on BOTH the artist and the title ("Low Rider" is not "Flamingo Rider")
    if best and score >= 0.75 and not a and not used[0]:
        # only a song title was typed: fix its spelling, but leave WHO it's by to YouTube's most-played
        # uploads. The catalog's top hit can be someone's cover ("Friends in Low Places" → Cassyette, while
        # Garth Brooks isn't on the streaming catalogs at all)
        global CATALOG_NAME
        CATALOG_NAME = None
        return "", best[1], _norm(best[1]) != _norm(t)
    if best and score >= 0.75:
        CATALOG_NAME = best
        t0 = used[1]
        # keep version words the person typed that the catalog title leaves out (Remix, Clean, Intro...)
        extra = re.findall(r"\((?:[^)]*(?:remix|mix|edit|clean|dirty|intro|acapella|instrumental|version)[^)]*)\)", t0, re.I)
        title = best[1] + "".join(" " + e for e in extra if e.lower() not in best[1].lower())
        fixed = _norm(best[0]) != _norm(a) or _norm(title) != _norm(t)
        return best[0], title, fixed
    return a, t, False


def split_query(q):
    """Song names typed or pasted any common way. Returns (artist, title).
       Title - Artist          (the default when a dash could go either way)
       Artist - Title          (used when the music catalog says that's the real order)
       Title by Artist         "Title" by Artist      Title, by Artist      Title (by Artist)
       Artist: Title           Artist "Title"         "Title" Artist        Title | Artist   Title / Artist"""
    q = _clean_query(q)
    dash = any(sep in q for sep in (" - ", " – ", " — ", " | "))
    m = re.match(r'^(.*?),?\s*\(?\bby\s+(.+?)\)?$', q, re.I)          # ... by Artist
    if m and m.group(1).strip() and not dash:
        a, t = m.group(2).strip(' "'), m.group(1).strip(' "')
        # "Stand By Me" is a title, not "Stand" by "Me": keep it whole when the catalog says so
        if _deezer_hit(a, t) or not _deezer_hit("", q):
            return a, t
    m = re.match(r'^"(.+?)"\s*[-–—,:]?\s*(.+)$', q)                     # "Title" Artist
    if m:
        return m.group(2).strip(' "'), m.group(1).strip()
    m = re.match(r'^(.+?)\s*[-–—,:]?\s*"(.+?)"$', q)                     # Artist "Title"
    if m:
        return m.group(1).strip(' "'), m.group(2).strip()
    m = re.match(r'^([^:]{2,40}):\s+(.+)$', q)                            # Artist: Title
    if m and "http" not in q:
        return m.group(1).strip(), m.group(2).strip()
    if q.count("-") == 1 and not any(x in q for x in (" - ", " -", "- ")):
        l_, r_ = q.split("-")
        if " " in l_.strip() and " " in r_.strip():           # "uptwon funk-mark ronsen"
            q = l_.strip() + " - " + r_.strip()
    for sep in (" - ", " – ", " — ", " | ", " / ", " -", "- "):
        if sep in q:
            left, right = (x.strip() for x in q.split(sep, 1))
            # default Title - Artist; flip only when the catalog knows Artist - Title and not the other
            if _deezer_hit(left, right) and not _deezer_hit(right, left):
                return left, right
            return right, left
    return "", q


# --------------------------------------------------------------------------- serato crates
#
# Serato DJ Pro 4 keeps each drive's library in _Serato_/Library/location.sqlite, and also
# syncs with the classic .crate files in _Serato_/Subcrates: it notices new or changed crate
# files when it starts. We never touch the database. We only add tracks to a crate file,
# and only while Serato is closed (Serato rewrites every crate file when it quits).

PENDING = APP_SUPPORT / "pending_crates.json"
CRATE_BACKUPS = APP_SUPPORT / "crate-backups"
CRATE_VERSION = "1.0/Serato ScratchLive Crate"


def volume_root(path):
    parts = Path(path).resolve().parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/", "Volumes", parts[2])
    return None


def check_drive(outdir):
    """Refuse to save to a USB path while the USB is unplugged (macOS would otherwise
    quietly create a look-alike folder on the internal drive)."""
    p = Path(os.path.expanduser(str(outdir)))
    parts = p.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        root = Path("/", "Volumes", parts[2])
        if not os.path.ismount(root):
            raise RuntimeError(f"{parts[2]} isn't plugged in. Connect it and try again.")


SERATO_SEEN = APP_SUPPORT / "serato_seen"


def serato_running():
    """True while Serato is open, and for a minute after it closes (it can still be saving its library as it
    quits; touching the library then is what broke it once)."""
    if WINDOWS:
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
        up = "serato dj" in out
    else:
        up = subprocess.run(["pgrep", "-if", r"Serato DJ (Pro|Lite)"], capture_output=True).returncode == 0
    if up:
        try:
            SERATO_SEEN.parent.mkdir(parents=True, exist_ok=True); SERATO_SEEN.write_text(str(time.time()))
        except OSError:
            pass
        return True
    try:
        return time.time() - float(SERATO_SEEN.read_text()) < 60
    except (OSError, ValueError):
        return False


def _db3_ok(c):
    """The checks Serato runs when it opens its library: no damage and every row's links intact. Rows left
    pointing at a song entry that no longer exists (analysis data of a removed track) are cleared first."""
    for _ in range(3):
        bad = c.execute("pragma db3.foreign_key_check").fetchall()
        if not bad:
            break
        for t, rowid, parent, fk in bad:
            if t in ("dj_asset_metadata", "asset_auxiliary") and parent == "asset":
                c.execute(f'delete from db3."{t}" where rowid=?', (rowid,))
        c.commit()
    return (c.execute("pragma db3.integrity_check").fetchone()[0] == "ok"
            and not c.execute("pragma db3.foreign_key_check").fetchall())


def _tag(name, payload):
    import struct
    return name.encode("ascii") + struct.pack(">I", len(payload)) + payload


def _u16(s):
    return s.encode("utf-16-be")


def _walk(b):
    import struct
    i = 0
    while i + 8 <= len(b):
        tag = b[i:i + 4].decode("latin1")
        ln = struct.unpack(">I", b[i + 4:i + 8])[0]
        yield tag, b[i + 8:i + 8 + ln], i
        i += 8 + ln


def crate_tracks(data):
    out = []
    for tag, val, _ in _walk(data):
        if tag == "otrk":
            for t2, v2, _ in _walk(val):
                if t2 == "ptrk":
                    out.append(v2.decode("utf-16-be"))
    return out


def crate_header(subcrates):
    """Header (version, sort, columns) copied from one of your existing crates so the new
    crate shows the same columns. Falls back to a bare version header."""
    for f in sorted(subcrates.glob("*.crate")):
        try:
            b = f.read_bytes()
        except Exception:
            continue
        for tag, _, pos in _walk(b):
            if tag == "otrk":
                if pos > 0:
                    return b[:pos]
                break
    return _tag("vrsn", _u16(CRATE_VERSION))


HOLIDAYS = [(r"christmas|xmas|santa claus|mistletoe|jingle bell|sleigh|rudolph|frosty|winter wonderland|"
             r"silent night|feliz navidad|carol of the bells|holly jolly|let it snow", "Christmas"),
            (r"halloween|monster mash|ghostbusters|spooky|thriller blend", "Halloween"),
            (r"thanksgiving", "Thanksgiving"), (r"hanukkah|chanukah|dreidel", "Hanukkah"),
            (r"new year|auld lang syne", "New Year"), (r"valentine'?s day", "Valentine's Day"),
            (r"easter bunny|happy easter", "Easter"), (r"4th of july|fourth of july", "4th of July")]


def holiday_folder(title):
    """Holidays/<holiday> for a holiday song (by its title), else None. Owner library only."""
    t = (title or "").lower()
    for pat, name in HOLIDAYS:
        if re.search(pat, t):
            return f"Holidays/{name}"
    return None


def crate_name_for(folder, root):
    rel = Path(folder).resolve().relative_to(root.resolve())
    parts = list(rel.parts)
    tops = {p.split("/")[0] for p in GENRE_PATH.values()} | {"Holidays"}
    # a track filed into a genre folder goes into the matching GENRES crate
    if len(parts) >= 2 and parts[1] in tops and library_root(Path(root, parts[0], "x")):
        return "%%".join(["GENRES"] + parts[1:])
    return "%%".join(parts)


class _CrateLock:
    """Only one DJ Grab process touches crate files or the pending list at a time."""
    def __enter__(self):
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        self.f = open(APP_SUPPORT / "crates.lock", "w")
        if WINDOWS:
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1); break
                except OSError:
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        if WINDOWS:
            import msvcrt
            try:
                self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def queue_for_crate(track_path, extra_crate=None):
    with _CrateLock():
        return _queue_for_crate(track_path, extra_crate)


def _queue_for_crate(track_path, extra_crate=None):
    root = volume_root(track_path)
    if not root or not (root / "_Serato_" / "Subcrates").is_dir():
        return None  # not on a Serato drive
    track = str(Path(track_path).resolve().relative_to(root.resolve()))
    crates = [crate_name_for(Path(track_path).parent, root)]
    if extra_crate:
        crates.append(extra_crate)
    pending = load_pending()
    for cr in crates:
        entry = {"volume": str(root), "crate": cr, "track": track}
        if entry not in pending:
            pending.append(entry)
    save_pending(pending)
    return crates[0]


def load_pending():
    try:
        return json.loads(PENDING.read_text())
    except Exception:
        return []


def save_pending(items):
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    if items:
        tmp = PENDING.with_suffix(".tmp"); tmp.write_text(json.dumps(items, indent=1)); tmp.replace(PENDING)
    elif PENDING.exists():
        PENDING.unlink()


def flush_crates():
    """Write queued tracks into their crate files if Serato is closed.
    Returns (added, still_pending)."""
    with _CrateLock():
        return _flush_crates()


NAMES_CACHE = APP_SUPPORT / "serato_names_checked.json"
VOLUMES = Path("/Volumes")          # macOS drives (owner library features; nothing on Windows)


def restore_grids():
    """DJ Grab intro files carry our beatgrid. When Serato first imports one it may analyse it anyway and
    swap in its own rounded tempo. Once per file, after Serato has picked it up: put our grid back and lock
    it in Serato so it stays (after that, any change you make yourself is left alone).
    Only while Serato is closed. Returns how many grids were restored."""
    import base64, sqlite3, unicodedata
    from mutagen.id3 import ID3, GEOB
    watch_f = APP_SUPPORT / "grid_watch.json"
    if serato_running() or not watch_f.exists():
        return 0
    try:
        watch = json.loads(watch_f.read_text())
    except Exception:
        return 0
    nfc = lambda x: unicodedata.normalize("NFC", x or "").lower()
    by_vol = {}
    now = time.time()
    for path, w in list(watch.items()):
        f = Path(path)
        if not f.exists() or now - w.get("t", now) > 30 * 86400:
            watch.pop(path, None)
            continue
        vol = volume_root(path)
        if vol is not None and os.path.ismount(vol):
            rel = nfc(str(f.resolve().relative_to(Path(vol).resolve())))
            by_vol.setdefault(Path(vol), {})[rel] = (path, w)
    fixed = 0
    ours_f = APP_SUPPORT / "locked_by_djgrab.json"
    try:
        ours = set(json.loads(ours_f.read_text()))
    except Exception:
        ours = set()
    for vol, items in by_vol.items():
        db = vol / "_Serato_" / "Library" / "location.sqlite"
        if not db.is_file():
            continue
        work = APP_SUPPORT / "location.lock.sqlite"
        shutil.copy2(db, work)
        c = sqlite3.connect(":memory:")
        c.execute("ATTACH DATABASE ? AS db3", (str(work),))
        seen = {nfc(pid): (aid, lk) for aid, pid, lk in
                c.execute("select id, portable_id, is_beatgrid_locked from db3.asset")}
        to_lock, finished = [], []
        for rel, (path, w) in items.items():
            if rel not in seen:
                continue                                 # Serato hasn't picked it up yet: check next time
            f, want = Path(path), base64.b64decode(w["grid"])
            try:
                t = ID3(str(f))
                have = next((g.data for g in t.getall("GEOB") if g.desc == "Serato BeatGrid"), b"")
                if have != want:
                    t.delall("GEOB:Serato BeatGrid")
                    t.add(GEOB(encoding=0, mime="application/octet-stream", desc="Serato BeatGrid", data=want))
                    tmp = f.with_name("." + f.name + ".djgrab-tmp")
                    shutil.copy2(f, tmp)
                    t.save(str(tmp), v2_version=3 if t.version[1] == 3 else 4)
                    os.replace(tmp, f)
                    fixed += 1
            except Exception:
                continue
            aid, lk = seen[rel]
            if not lk:
                to_lock.append(aid)
                ours.add(rel)
            finished.append(path)
        ok_db = True
        if to_lock:
            R = c.execute("select revision from db3.serato").fetchone()[0] + 1
            c.execute("update db3.serato set revision=?", (R,))
            for aid in to_lock:
                c.execute("update db3.asset set is_beatgrid_locked=1, revision=? where id=?", (R, aid))
            c.execute("update db3.space set revision=? where id=1", (R,))
            c.commit()
            ok_db = _db3_ok(c) and not serato_running()
            if ok_db:
                bk = CRATE_BACKUPS / time.strftime("%Y%m%d-%H%M%S")
                bk.mkdir(parents=True, exist_ok=True)
                shutil.copy2(db, bk / "location.sqlite")
                tmp = db.with_name("location.sqlite.djgrab-tmp")
                shutil.copy2(work, tmp)
                os.replace(tmp, db)
        c.close()
        work.unlink(missing_ok=True)
        if ok_db:
            for path in finished:
                watch.pop(path, None)
    tmp = watch_f.with_suffix(".tmp"); tmp.write_text(json.dumps(watch)); tmp.replace(watch_f)
    tmp = ours_f.with_suffix(".tmp"); tmp.write_text(json.dumps(sorted(ours))); tmp.replace(ours_f)
    return fixed


def fix_serato_names():
    """Serato leaves Name and Artist blank for tracks it picks up from a crate file, and then
    shows the whole file name ("Artist - Title") in the Name column. Fill them in from the
    song's own tags. Only while Serato is closed; the database is edited on a copy and swapped
    in whole, with a backup kept. Returns how many tracks were fixed."""
    import sqlite3
    from mutagen.id3 import ID3
    if serato_running():
        return 0
    try:
        checked = json.loads(NAMES_CACHE.read_text())
    except Exception:
        checked = {}
    fixed = 0
    for vol in (VOLUMES.iterdir() if VOLUMES.is_dir() else []):
        db = vol / "_Serato_" / "Library" / "location.sqlite"
        if not db.is_file() or not os.path.ismount(vol):
            continue
        work = APP_SUPPORT / "location.work.sqlite"
        shutil.copy2(db, work)
        c = sqlite3.connect(":memory:")
        c.execute("ATTACH DATABASE ? AS db3", (str(work),))
        rows = c.execute("select id, portable_id, artist, name, album, genre from db3.asset "
                         "where name is null or name = ''").fetchall()
        updates = []
        for aid, pid, artist, name, album, genre in rows:
            f = vol / pid
            key = f"{vol.name}/{pid}"
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if checked.get(key) == mt:
                continue                      # no tags last time and the file hasn't changed
            try:
                t = ID3(str(f))
            except Exception:
                checked[key] = mt; continue
            val = lambda k: (str(t[k].text[0]).strip() if k in t and t[k].text else "")
            title, art = val("TIT2"), val("TPE1")
            if not title:
                checked[key] = mt; continue
            updates.append((title, artist or art, album or val("TALB"), genre or val("TCON"), aid))
        if updates:
            R = c.execute("select revision from db3.serato").fetchone()[0] + 1
            c.execute("update db3.serato set revision=?", (R,))
            for title, art, alb, gen, aid in updates:
                c.execute("update db3.asset set name=?, artist=?, album=?, genre=?, revision=? where id=?",
                          (title, art, alb, gen, R, aid))
            c.execute("update db3.space set revision=? where id=1", (R,))
            c.commit()
            ok = "ok" if _db3_ok(c) else "bad"
            c.close()
            if ok == "ok" and not serato_running():
                bk = CRATE_BACKUPS / time.strftime("%Y%m%d-%H%M%S")
                bk.mkdir(parents=True, exist_ok=True)
                shutil.copy2(db, bk / "location.sqlite")
                tmp = db.with_name("location.sqlite.djgrab-tmp")
                shutil.copy2(work, tmp)
                os.replace(tmp, db)
                fixed += len(updates)
        else:
            c.close()
        work.unlink(missing_ok=True)
    tmp = NAMES_CACHE.with_suffix(".tmp"); tmp.write_text(json.dumps(checked)); tmp.replace(NAMES_CACHE)
    return fixed



# --------------------------------------------------------------------------- library renames
RENAMES = Path(__file__).resolve().parent / "renames.json"
RENAMES_DONE = APP_SUPPORT / "renames_done.json"


def _nfc(x):
    import unicodedata
    return unicodedata.normalize("NFC", x)


def audit_genres(budget=75.0):
    """Library-wide genre check, a little at a time (run from the --fix-names timer while the app is open):
    looks every song's genre up online and records where it sits vs where the lookup puts it, in
    .djgrab/logs/genre_audit.json. Moves nothing: the list is reviewed first. Requested by creating
    .djgrab/audit_genres.request; removes the request when the whole library is done."""
    here = Path(__file__).resolve().parent
    req = here / "audit_genres.request"
    if not req.exists():
        return 0
    out = here / "logs" / "genre_audit.json"
    out.parent.mkdir(exist_ok=True)
    try:
        res = json.loads(out.read_text())
    except Exception:
        res = {}
    from mutagen.id3 import ID3
    t0, n = time.time(), 0
    for vol in (VOLUMES.iterdir() if VOLUMES.is_dir() else []):
        root = vol / vol.name
        if not root.is_dir() or not os.path.ismount(vol):
            continue
        for top in sorted(os.listdir(root)):
            if top.startswith((".", "_", "00")) or top in ("Tools", "Other", "Mixes") or not (root / top).is_dir():
                continue
            for dp, dn, fn_ in os.walk(root / top):
                dn[:] = sorted(d for d in dn if not d.startswith((".", "_")))
                for f in sorted(fn_):
                    if f.startswith("._") or not f.lower().endswith(".mp3") or f.endswith("(Intro).mp3"):
                        continue
                    rel = _nfc(str(Path(dp, f).relative_to(vol)))
                    if rel in res:
                        continue
                    if time.time() - t0 > budget:
                        tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(res, indent=0)); tmp.replace(out)
                        return n
                    try:
                        t = ID3(str(Path(dp, f)))
                        art = str(t["TPE1"].text[0]) if "TPE1" in t else ""
                        tit = str(t["TIT2"].text[0]) if "TIT2" in t else Path(f).stem
                    except Exception:
                        art, tit = "", Path(f).stem
                    if not art and " - " in Path(f).stem:
                        art, tit = Path(f).stem.split(" - ", 1)
                    code = lookup_genre(art, tit) if art else None
                    res[rel] = {"artist": art, "title": tit, "folder": str(Path(dp).relative_to(root)),
                                "online": GENRE_PATH.get(code) if code else None}
                    n += 1
    tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(res, indent=0)); tmp.replace(out)
    req.unlink(missing_ok=True)
    return n


# ------------------------------------------------------------------ name cleanup (per folder)
_VERSION_WORDS = {"clean", "dirty", "explicit", "intro", "outro", "instrumental", "acapella", "acappella",
                  "acap", "in", "out", "remix", "edit", "radio", "extended", "short", "quick", "hit", "mix",
                  "original", "club", "dub", "hh", "transition", "redrum", "break", "drop", "hype", "party",
                  "starter", "version", "vip", "bootleg", "mashup", "snip", "super", "clap", "hook", "verse",
                  "and", "&", "mixshow", "jersey", "slam", "rmx", "16", "32", "8", "bars", "bar", "beat"}
_JUNK_COMMENT = re.compile(r"(https?://|www\.|\.(com|net|org|io)\b)", re.I)
_POOL_ALBUM = re.compile(r"^@|\bpool\b|https?://|www\.|\.(com|net)\b", re.I)


def _label_case(title):
    """Version labels in one style: "(CLEAN) (INTRO)" -> "(Clean) (Intro)". Only groups made of version
    words change; part of the song's own name ("(CL Solo)", "(Taylor's Version)") stays as written."""
    def fix(m):
        inner = m.group(1)
        words = re.findall(r"[A-Za-z0-9&]+", inner)
        if words and inner.upper() == inner and all(w.lower() in _VERSION_WORDS for w in words):
            return "(" + " ".join(w if w.isdigit() or w == "&" or w.upper() == "HH" else w.capitalize()
                                  for w in re.split(r"\s+", inner.strip())) + ")"
        return m.group(0)
    return re.sub(r"\(([^()]*)\)", fix, title)


def _alnum(x):
    return re.sub(r"[^a-z0-9]+", "", fold(x or ""))


def official_spelling(artist, title):
    """(artist's name, song title) as the official release spells them, from Deezer, but only where they
    are the same words as what we have (case, accents and punctuation: "APT" -> "APT.", "ROSE" -> "ROSÉ",
    "GANGNAM STYLE" -> "Gangnam Style"). Anything that would change the words is left alone. Cached."""
    import urllib.parse
    base = re.sub(r"\s*[\(\[].*$", "", title).strip()
    if not artist or not base:
        return None, None
    try:
        cache = json.loads(SPELLING.read_text())
    except Exception:
        cache = {}
    key = f"{_alnum(artist)}|{_alnum(base)}"
    if key in cache and not _retry(cache[key]):
        v = cache[key]
        return (v[0], v[1]) if isinstance(v, list) else (None, None)
    pa = re.split(r"\s+(?:ft\.?|feat\.?|featuring|x|&|and|with|vs\.?)\s+|,|;|/", artist, flags=re.I)[0].strip()
    found, failed = None, False
    try:
        q = urllib.parse.quote(f'artist:"{pa}" track:"{base}"')
        for h in _get_json(f"https://api.deezer.com/search?q={q}&limit=10").get("data") or []:
            name = (h.get("artist") or {}).get("name", "")
            t = re.sub(r"\s*[\(\[].*$", "", h.get("title") or "").strip()
            if _alnum(name) == _alnum(pa) and _alnum(t) == _alnum(base):
                found = [name, t]
                break
    except Exception as ex:
        found, failed = None, True
        _clog(f"spelling lookup error: {type(ex).__name__}: {ex}")
    _clog(f"spelling {artist} - {title}: " + ("lookup failed" if failed else (f"{found[0]} - {found[1]}" if found else "no match")))
    if failed:
        return None, None                          # couldn't reach Deezer: ask again next time
    cache[key] = found if found else {"none": time.time()}
    try:
        tmp = SPELLING.with_suffix(".tmp"); tmp.write_text(json.dumps(cache)); tmp.replace(SPELLING)
    except Exception:
        pass
    return (found[0], found[1]) if found else (None, None)


def _clog(msg):
    try:
        f = Path(__file__).resolve().parent / "logs" / "cleanup.log"
        f.parent.mkdir(exist_ok=True)
        with open(f, "a") as h:
            h.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


SPELLING = APP_SUPPORT / "spelling2.json"     # v2: lookups that failed are no longer saved as "not found"


def _bitrate(p):
    try:
        from mutagen.mp3 import MP3
        i = MP3(str(p)).info
        return i.bitrate or 0, i.length or 0.0
    except Exception:
        return 0, 0.0


_NAME_SPLIT = re.compile(r"\s+(?:ft\.?|feat\.?|featuring|x|&|and|with|vs\.?)\s+|,\s*|;|/", re.I)


def _artists_out_of_title(title, artist):
    """Names belong in the artist tag only. "ExtraL (feat. Doechii)" -> title "ExtraL", and Doechii goes
    into the artist if it isn't there yet ("JENNIE feat. Doechii"); "Give It To Me - Timbaland" -> "Give It
    To Me" when Timbaland is one of the artists. Returns (title, artist)."""
    names = lambda a: {_alnum(n) for n in _NAME_SPLIT.split(a or "") if n.strip()}
    have = names(artist)
    m = re.search(r"\s*[\(\[](?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", title, re.I)
    if m:
        guests = [n.strip() for n in _NAME_SPLIT.split(m.group(1)) if n.strip()]
        new = [n for n in guests if _alnum(n) not in have]
        if new:
            joiner = " & " if re.search(r"\b(feat\.?|ft\.?|featuring)\s", artist or "", re.I) else " feat. "
            artist = f"{artist}{joiner}{' & '.join(new)}" if artist else " & ".join(new)
            have |= {_alnum(n) for n in new}
        title = (title[:m.start()] + title[m.end():]).strip()
    m = re.match(r"^(.*?)\s+-\s+([^()\[\]]+?)(\s*[\(\[].*)?$", title)
    if m and _alnum(m.group(2)) in have:
        title = (m.group(1) + (m.group(3) or "")).strip()
    return title, artist


def plan_cleanup(folder):
    """Tidy a folder's names and tags, queued in renames.json (done by the rename step while Serato is
    closed, which also updates Serato's library and crates):
      1. version labels in one style, titles spelled the official way
      2. file names "Artist - Title (Labels).mp3" matching the tags, artist spelled the official way
      3. album: DJ pool watermarks taken out (a year as the album stays, and is copied to the Year tag)
      4. comment: download-site text taken out (kept in a hidden tag, so it can be put back)
      5. duplicates: the same song AND the same version (both clean, both intros...) -> the best copy
         (higher bitrate, then more plays) stays, the others move to _To Delete/Duplicates. An original and
         an intro, or a clean and a dirty, are different versions and both stay. DJ Grab intros never count.
    Returns how many items were queued."""
    from mutagen.id3 import ID3
    folder = Path(folder)
    vol = volume_root(str(folder))
    if vol is None:
        return 0
    vol = Path(vol)
    items, groups = [], {}
    for p in sorted(folder.glob("*.mp3")):
        if p.name.startswith("."):
            continue
        try:
            t = ID3(str(p))
        except Exception:
            continue
        g = lambda k: str(t[k]).strip() if k in t else ""
        artist = g("TPE1") or p.stem.partition(" - ")[0]
        title = g("TIT2") or p.stem.partition(" - ")[2] or p.stem
        if artist and title.lower().startswith(artist.lower() + " - "):
            title = title[len(artist) + 3:]
        ours = "TXXX:DJ Grab intro" in t
        new_title, artist_x = _artists_out_of_title(_label_case(title), artist)
        oa, ot = official_spelling(artist, new_title)
        m = re.match(r"^(\s*)(.*?)(\s*[\(\[].*)?$", new_title)
        base, rest = m.group(2), m.group(3) or ""
        if ot and ot != base and _alnum(ot) == _alnum(base):
            new_title = ot + rest
        new_artist = artist_x
        pa = re.split(r"\s+(?:ft\.?|feat\.?|featuring|x|&|and|with|vs\.?)\s+|,|;|/", artist, flags=re.I)[0].strip()
        if oa and oa != pa and _alnum(oa) == _alnum(pa):
            new_artist = oa + artist_x[len(pa):]
        x = {"volume": vol.name, "old": str(p.relative_to(vol)), "artist": new_artist, "title": new_title,
             "no_crate": True, "cleanup": True}
        name = re.sub(r'[/\\:*?"<>|]', "-", f"{new_artist} - {new_title}").rstrip(". ") + p.suffix
        if name != p.name and not (re.sub(r" \(\d+\)$", "", p.stem) + p.suffix == name):
            x["new"] = str(p.with_name(name).relative_to(vol))
        album = g("TALB")
        old_album = g("TXXX:DJ Grab old album")
        if album and _POOL_ALBUM.search(album):
            x["album"] = ""                           # a DJ pool's watermark, not an album
        elif not album and re.fullmatch(r"(19|20)\d\d", old_album):
            x["album"] = old_album                    # a year as the album is fine: put back what an earlier cleanup took out
        if re.fullmatch(r"(19|20)\d\d", album or old_album) and not (g("TDRC") or g("TYER")):
            x["year"] = album or old_album
        shown = " | ".join(str(c) for c in t.getall("COMM") if not c.desc and str(c).strip())
        djg = next((str(v) for v in t.getall("TXXX:DJ Grab comment")), "")
        user = shown[:len(shown) - len(djg)].rstrip(" ·,") if djg and shown.endswith(djg) else shown
        parts = [q.strip() for q in re.split(r"\s*(?:,|·|\|)\s*", user) if q.strip()]
        junk = [q for q in parts if _JUNK_COMMENT.search(q)]
        if junk:
            x["comment_drop"] = junk
        # one style for every comment, also on songs that weren't redone: things split by commas, no dots
        # or bars, each thing once, no links ("KPop,Mainstream · 2010s" -> "KPop, Mainstream, 2010s")
        tidy = []
        for q in re.split(r"\s*(?:,|·|\|)\s*", shown):
            if q and q not in tidy and not _JUNK_COMMENT.search(q):
                tidy.append(q)
        if shown and ", ".join(tidy) != shown:
            x["comment_set"] = ", ".join(tidy)
            if djg:
                x["comment_ours"] = ", ".join(q for q in re.split(r"\s*(?:,|·|\|)\s*", djg) if q)
        changed = ("new" in x or new_title != title or new_artist != artist or g("TIT2") != new_title
                   or "album" in x or "comment_drop" in x or "comment_set" in x)
        if changed:
            x.setdefault("new", x["old"])
            items.append(x)
        if not ours:
            labels = tuple(sorted(w.lower() for w in re.findall(r"\(([^()]*)\)", _label_case(rest))))
            groups.setdefault((_alnum(primary_artist(new_artist)), _alnum(new_title.split("(")[0]), labels), []).append(
                (p, x if changed else None, int(g("TXXX:SERATO_PLAYCOUNT") or 0) if g("TXXX:SERATO_PLAYCOUNT").isdigit() else 0))
    for key, ps in groups.items():
        if len(ps) < 2:
            continue
        info = [(p, x, plays) + _bitrate(p) for p, x, plays in ps]
        lens = [i[4] for i in info]
        if max(lens) - min(lens) > 15:
            continue                                  # different lengths: different edits, not copies
        top = max(i[3] for i in info) or 1
        best = max(info, key=lambda i: (i[3] >= 0.9 * top, i[2], i[3], i[4]))
        for p, x, plays, br, ln in info:
            if p == best[0]:
                continue
            rel = str((vol / "_To Delete" / "Duplicates" / p.name).relative_to(vol))
            if x is None:
                x = {"volume": vol.name, "old": str(p.relative_to(vol)), "no_crate": True, "cleanup": True}
                try:
                    t = ID3(str(p))
                    x["artist"], x["title"] = str(t.get("TPE1", "")), str(t.get("TIT2", p.stem))
                except Exception:
                    x["artist"], x["title"] = "", p.stem
                items.append(x)
            x["new"] = rel
            x["duplicate_of"] = best[0].name
    if not items:
        return 0
    try:
        cur = json.loads(RENAMES.read_text()) if RENAMES.exists() else []
        done = set(json.loads(RENAMES_DONE.read_text())) if RENAMES_DONE.exists() else set()
    except Exception:
        return 0
    pending = {x["old"] for x in cur if x["old"] not in done}
    add = [x for x in items if x["old"] not in pending]
    # an item done before can come back (a song re-tidied after a later change): forget the old "done"
    done -= {x["old"] for x in add}
    cur = [c for c in cur if c["old"] not in {x["old"] for x in add}] + add
    tmp = RENAMES.with_suffix(".tmp"); tmp.write_text(json.dumps(cur, indent=1, ensure_ascii=False)); tmp.replace(RENAMES)
    tmp = RENAMES_DONE.with_suffix(".tmp"); tmp.write_text(json.dumps(sorted(done))); tmp.replace(RENAMES_DONE)
    return len(add)


def _cleanup_tags(t, x):
    """The album / year / comment parts of a cleanup item. What's taken out of the comment is kept in a
    hidden tag ("DJ Grab removed comment") so it can be put back."""
    from mutagen.id3 import TALB, TDRC, COMM, TXXX
    if "album" in x:
        old = str(t.get("TALB", ""))
        if old and not x["album"]:
            t.add(TXXX(encoding=3, desc="DJ Grab old album", text=old))
            t.delall("TALB")
        elif x["album"]:
            t.setall("TALB", [TALB(encoding=3, text=x["album"])])
            if str(t.get("TXXX:DJ Grab old album", "")) == x["album"]:
                t.delall("TXXX:DJ Grab old album")
    if x.get("year") and not (t.getall("TDRC") or t.getall("TYER")):
        t.setall("TDRC", [TDRC(encoding=3, text=str(x["year"]))])
    drop = x.get("comment_drop") or []
    if drop:
        mine = [c for c in t.getall("COMM") if not c.desc]
        shown = " | ".join(str(c) for c in mine if str(c).strip())
        djg = next((str(v) for v in t.getall("TXXX:DJ Grab comment")), "")
        user = shown[:len(shown) - len(djg)].rstrip(" ·,") if djg and shown.endswith(djg) else shown
        parts = [q.strip() for q in re.split(r"\s*(?:,|·|\|)\s*", user) if q.strip()]
        keep = [q for q in parts if q not in drop]
        gone = [q for q in parts if q in drop]
        if gone:
            prev = next((str(v) for v in t.getall("TXXX:DJ Grab removed comment")), "")
            t.setall("TXXX:DJ Grab removed comment", [TXXX(encoding=3, desc="DJ Grab removed comment",
                                                           text=" | ".join(q for q in [prev] + gone if q))])
            new = ", ".join(q for q in (", ".join(keep), djg) if q)
            for c in mine:
                t.delall(c.HashKey)
            if new:
                t.add(COMM(encoding=3, lang="eng", desc="", text=new))


def _cleanup_comment_set(t, x):
    from mutagen.id3 import COMM, TXXX
    if "comment_set" not in x:
        return
    links = [q for q in re.split(r"\s*(?:,|·|\|)\s*", " | ".join(str(c) for c in t.getall("COMM") if not c.desc))
             if q and _JUNK_COMMENT.search(q)]
    if links:
        prev = next((str(v) for v in t.getall("TXXX:DJ Grab removed comment")), "")
        t.setall("TXXX:DJ Grab removed comment", [TXXX(encoding=3, desc="DJ Grab removed comment",
                                                       text=" | ".join(q for q in [prev] + links if q and q not in prev))])
    for c in [c for c in t.getall("COMM") if not c.desc]:
        t.delall(c.HashKey)
    if x["comment_set"]:
        t.add(COMM(encoding=3, lang="eng", desc="", text=x["comment_set"]))
    if x.get("comment_ours"):
        t.setall("TXXX:DJ Grab comment", [TXXX(encoding=3, desc="DJ Grab comment", text=x["comment_ours"])])


def apply_renames():
    """Rename songs listed in renames.json (cleaner "Artist - Title"), fix their tags, and update
    Serato's database, crates and database V2 to match. Only while Serato is closed. Safe to
    re-run: finished items are remembered, and half-done runs are picked up again."""
    import sqlite3
    from mutagen.id3 import ID3, TIT2, TPE1
    if not RENAMES.exists() or serato_running():
        return 0
    try:
        items = json.loads(RENAMES.read_text())
        done = set(json.loads(RENAMES_DONE.read_text())) if RENAMES_DONE.exists() else set()
    except Exception:
        return 0
    todo = [x for x in items if x.get("new") and x["old"] not in done]
    # duplicates leave first, so the copy that stays can take the plain name
    todo.sort(key=lambda x: not x["new"].startswith("_To Delete/"))
    if not todo:
        return 0
    by_vol = {}
    for x in todo:
        by_vol.setdefault(x.get("volume", "Vinny's USB"), []).append(x)
    total = 0
    for volname, group in by_vol.items():
        vol = VOLUMES / volname
        if not os.path.ismount(vol):
            continue
        move, meta, finished = {}, {}, []
        for x in group:
            src, dst = vol / x["old"], vol / x["new"]
            if not src.exists():
                if dst.exists():                        # renamed in an earlier run; Serato may still need it
                    move[_nfc(x["old"])] = x["new"]; meta[x["new"]] = x
                finished.append(x["old"])
                continue
            same = src.exists() and dst.exists() and os.path.samefile(src, dst)
            if dst.exists() and not same and not (_nfc(str(src)).lower() == _nfc(str(dst)).lower()):
                base, ext = os.path.splitext(x["new"]); n = 2
                base = re.sub(r" \(\d+\)$", "", base)
                while (vol / f"{base} ({n}){ext}").exists():
                    n += 1
                x = dict(x, new=f"{base} ({n}){ext}"); dst = vol / x["new"]
            try:
                if src.suffix.lower() == ".mp3":
                    t = ID3(str(src))
                    if x.get("title"):
                        t.setall("TIT2", [TIT2(encoding=3, text=x["title"])])
                    if x.get("artist"):
                        t.setall("TPE1", [TPE1(encoding=3, text=x["artist"])])
                    _cleanup_tags(t, x)
                    _cleanup_comment_set(t, x)
                    t.save(str(src), v2_version=3)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
                if src.parent != dst.parent and not x.get("no_crate"):
                    _queue_for_crate(str(dst))            # its genre crate picks it up too
            except Exception as ex:
                emit("info", message=f"Couldn't rename {x['old']}: {ex}")
                continue
            move[_nfc(x["old"])] = x["new"]; meta[x["new"]] = x
            finished.append(x["old"])
        if move and not _serato_apply_moves(vol, move, meta):
            continue                                    # leave them for the next run to finish Serato's side
        done.update(finished)
        total += len(move)
        _follow_renames(vol, move)
    tmp = RENAMES_DONE.with_suffix(".tmp"); tmp.write_text(json.dumps(sorted(done))); tmp.replace(RENAMES_DONE)
    return total


def _follow_renames(vol, move):
    """The Intros & Cues record of finished songs follows a renamed file, so it isn't redone from scratch."""
    f = APP_SUPPORT / "intros_state.json"
    try:
        st = json.loads(f.read_text())
    except Exception:
        return
    pairs = {_nfc(str(vol / o)): _nfc(str(vol / n)) for o, n in move.items()}
    changed = False
    for sec in st.values():
        if isinstance(sec, dict):
            for o, n in pairs.items():
                if o in sec and n not in sec:
                    sec[n] = sec.pop(o); changed = True
    if changed:
        tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(st)); tmp.replace(f)


def _serato_apply_moves(vol, move, meta):
    """Point Serato at renamed files: location.sqlite, crate files and database V2."""
    import sqlite3
    S = vol / "_Serato_"
    if serato_running():
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bk = CRATE_BACKUPS / f"{stamp}-renames"
    bk.mkdir(parents=True, exist_ok=True)
    # 1) location.sqlite
    db = S / "Library" / "location.sqlite"
    if db.exists():
        work = APP_SUPPORT / "location.rename.sqlite"
        shutil.copy2(db, work); shutil.copy2(db, bk / "location.sqlite")
        c = sqlite3.connect(":memory:")
        c.execute("ATTACH DATABASE ? AS db3", (str(work),))
        R = c.execute("select revision from db3.serato").fetchone()[0] + 1
        c.execute("update db3.serato set revision=?", (R,))
        ids = {}
        for aid, pid in c.execute("select id, portable_id from db3.asset").fetchall():
            if _nfc(pid) in move:
                ids[aid] = move[_nfc(pid)]
        for aid in ids:                                  # two passes: names are unique ignoring case
            c.execute("update db3.asset set portable_id=? where id=?", (f"__djgrab_tmp__/{aid}", aid))
        for aid, new in ids.items():
            m = meta[new]
            c.execute("update db3.asset set portable_id=?, file_name=?, artist=?, name=?, revision=? where id=?",
                      (new, os.path.basename(new), m["artist"], m["title"], R, aid))
            if m.get("recrate"):                         # moved to another genre: out of its old crates
                for (sa,) in c.execute("select id from db3.space_asset where asset_id=?", (aid,)).fetchall():
                    c.execute("delete from db3.container_asset where space_asset_id=?", (sa,))
        c.execute("update db3.space set revision=? where id=1", (R,))
        c.commit()
        ok = "ok" if _db3_ok(c) else "bad"
        c.close()
        if ok != "ok" or serato_running():
            return False
        tmp = db.with_name("location.sqlite.djgrab-tmp"); shutil.copy2(work, tmp); os.replace(tmp, db)
        work.unlink(missing_ok=True)
    # 2) crate files
    sub = S / "Subcrates"
    if sub.is_dir():
        for f in sub.glob("*.crate"):
            data = f.read_bytes(); out = b""; changed = False
            for tag, val, _ in _walk(data):
                if tag == "otrk":
                    path = next((v.decode("utf-16-be") for t2, v, _ in _walk(val) if t2 == "ptrk"), None)
                    if path is not None and _nfc(path) in move:
                        changed = True
                        if meta[move[_nfc(path)]].get("recrate"):
                            continue                     # its new genre crate gets it instead
                        out += _tag("otrk", _tag("ptrk", _u16(move[_nfc(path)]))); continue
                out += _tag(tag, val)
            if changed:
                shutil.copy2(f, bk / f.name)
                tmp = f.with_name(f.name + ".djgrab-tmp"); tmp.write_bytes(out); tmp.replace(f)
    # 3) database V2
    v2 = S / "database V2"
    if v2.exists():
        data = v2.read_bytes(); out = b""; changed = False
        for tag, val, _ in _walk(data):
            if tag != "otrk":
                out += _tag(tag, val); continue
            fields = [(t2, v) for t2, v, _ in _walk(val)]
            path = next((v.decode("utf-16-be") for t2, v in fields if t2 == "pfil"), None)
            new = move.get(_nfc(path)) if path else None
            if not new:
                out += _tag(tag, val); continue
            m = meta[new]; repl = {"pfil": new, "tsng": m["title"], "tart": m["artist"]}
            body = b""; have = set()
            for t2, v in fields:
                if t2 in repl:
                    body += _tag(t2, _u16(repl[t2])); have.add(t2)
                else:
                    body += _tag(t2, v)
            for k in ("tsng", "tart"):
                if k not in have:
                    body += _tag(k, _u16(repl[k]))
            out += _tag("otrk", body); changed = True
        if changed:
            shutil.copy2(v2, bk / "database V2")
            tmp = v2.with_name("database V2.djgrab-tmp"); tmp.write_bytes(out); tmp.replace(v2)
    return True


def purge_serato_refs(vol, prefix="_To Delete/", pred=None):
    """Take everything under _To Delete out of Serato (library, crates, database V2). The files stay
    where they are until you empty that folder yourself. Only while Serato is closed."""
    import sqlite3
    vol = Path(vol)
    S = vol / "_Serato_"
    if serato_running() or not S.is_dir():
        return 0
    pre = _nfc(prefix).lower() if prefix else None
    if pred is None:
        hit = lambda p: p is not None and _nfc(p).lower().startswith(pre)
    else:
        hit = lambda p: p is not None and pred(p)
    removed = 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bk = CRATE_BACKUPS / f"{stamp}-purge"
    db = S / "Library" / "location.sqlite"
    if db.exists():
        work = APP_SUPPORT / "location.purge.sqlite"
        shutil.copy2(db, work)
        c = sqlite3.connect(":memory:")
        c.execute("ATTACH DATABASE ? AS db3", (str(work),))
        ids = [aid for aid, pid in c.execute("select id, portable_id from db3.asset").fetchall() if hit(pid)]
        live = [aid for aid in ids if c.execute("select 1 from db3.space_asset where asset_id=?", (aid,)).fetchone()]
        if live:
            R = c.execute("select revision from db3.serato").fetchone()[0] + 1
            c.execute("update db3.serato set revision=?", (R,))
            for aid in live:
                for (sa,) in c.execute("select id from db3.space_asset where asset_id=?", (aid,)).fetchall():
                    for cid, caid in c.execute("select container_id, id from db3.container_asset where space_asset_id=?", (sa,)).fetchall():
                        c.execute("delete from db3.container_asset where id=?", (caid,))
                        c.execute("update db3.container set revision=? where id=?", (R, cid))
                c.execute("delete from db3.space_asset where asset_id=?", (aid,))
            c.execute("update db3.space set revision=? where id=1", (R,))
            c.commit()
            ok = "ok" if _db3_ok(c) else "bad"
            c.close()
            if ok != "ok" or serato_running():
                return 0
            bk.mkdir(parents=True, exist_ok=True); shutil.copy2(db, bk / "location.sqlite")
            tmp = db.with_name("location.sqlite.djgrab-tmp"); shutil.copy2(work, tmp); os.replace(tmp, db)
            removed += len(live)
        else:
            c.close()
        work.unlink(missing_ok=True)
    sub = S / "Subcrates"
    for f in (sub.glob("*.crate") if sub.is_dir() else []):
        data = f.read_bytes(); out = b""; changed = False
        for tag, val, _ in _walk(data):
            if tag == "otrk" and hit(next((v.decode("utf-16-be") for t2, v, _ in _walk(val) if t2 == "ptrk"), None)):
                changed = True; continue
            out += _tag(tag, val)
        if changed:
            bk.mkdir(parents=True, exist_ok=True); shutil.copy2(f, bk / f.name)
            tmp = f.with_name(f.name + ".djgrab-tmp"); tmp.write_bytes(out); tmp.replace(f)
    v2 = S / "database V2"
    if v2.exists():
        data = v2.read_bytes(); out = b""; changed = False
        for tag, val, _ in _walk(data):
            if tag == "otrk" and hit(next((v.decode("utf-16-be") for t2, v, _ in _walk(val) if t2 == "pfil"), None)):
                changed = True; continue
            out += _tag(tag, val)
        if changed:
            bk.mkdir(parents=True, exist_ok=True); shutil.copy2(v2, bk / "database V2")
            tmp = v2.with_name("database V2.djgrab-tmp"); tmp.write_bytes(out); tmp.replace(v2)
    return removed


def _flush_crates():
    pending = load_pending()
    if not pending:
        return 0, 0
    if serato_running():
        return 0, len(pending)
    added = 0
    remaining = []
    groups = {}
    for e in pending:
        if not os.path.ismount(e["volume"]):
            remaining.append(e)  # drive unplugged: keep for later
            continue
        groups.setdefault((e["volume"], e["crate"]), []).append(e)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for (vol, crate), items in groups.items():
        sub = Path(vol) / "_Serato_" / "Subcrates"
        try:
            # Parent crates must exist for a nested crate to show up.
            names = crate.split("%%")
            for depth in range(1, len(names) + 1):
                name = "%%".join(names[:depth])
                f = sub / f"{name}.crate"
                if f.exists():
                    data = f.read_bytes()
                    bk = CRATE_BACKUPS / stamp
                    bk.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, bk / f.name)
                else:
                    data = crate_header(sub)
                if depth < len(names):
                    if not f.exists():
                        f.write_bytes(data)
                    continue
                have = set(crate_tracks(data))
                new = b""
                for e in items:
                    if e["track"] not in have and (Path(vol) / e["track"]).exists():
                        new += _tag("otrk", _tag("ptrk", _u16(e["track"])))
                        have.add(e["track"])
                        added += 1
                if new or not f.exists():
                    tmp = f.with_name(f.name + ".djgrab-tmp")
                    tmp.write_bytes(data + new)
                    tmp.replace(f)
        except Exception as ex:
            emit("info", message=f"Couldn't update crate {crate}: {ex}")
            remaining.extend(items)
    save_pending(remaining)
    return added, len(remaining)


# --------------------------------------------------------------------------- pipeline

def existing_file(outdir, base):
    for ext in (".mp3", ".aiff"):  # older AIFF grabs count as "already have it"
        p = outdir / f"{base}{ext}"
        if p.exists():
            return p
    return None


class HotUpload(Exception):
    """The upload is clipped/distorted (true peak far over 0 dB)."""
    def __init__(self, peak):
        super().__init__(f"distorted upload (peak {peak:+.1f} dB)")
        self.peak = peak


HOT_PEAK = 4.0


LOW_SEEN = []      # (link, quality score, reason) of uploads turned down for low quality in this search


class LowQualityUpload(HotUpload):
    """Below the minimum quality set in Settings."""
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.peak = 0




def high_cutoff(path):
    """The highest frequency with real music in it, from 30 s in the middle of the song (Hz)."""
    try:
        import numpy as np
        r = subprocess.run([FFMPEG, "-v", "quiet", "-ss", "45", "-t", "30", "-i", str(path), "-ac", "1",
                            "-ar", "44100", "-f", "f32le", "-"], capture_output=True)
        x = np.frombuffer(r.stdout, dtype=np.float32)
        if len(x) < 44100 * 5:
            return None
        S = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        f = np.fft.rfftfreq(len(x), 1 / 44100)
        ref = np.mean(S[(f >= 1000) & (f < 4000)]) + 1e-20
        cut = None
        for lo in range(10000, 20000, 500):
            band = np.mean(S[(f >= lo) & (f < lo + 500)])
            if 10 * np.log10(band / ref + 1e-30) > -38:      # still real sound up here
                cut = lo + 500
        return cut
    except Exception:
        return None


def process_entry(info, src_path, thumb_path, outdir, url, args):
    artist, title = parse_names(info)
    # a song found by name: use the catalog's spelling when it's clearly the same name ("The B 52's" from
    # the upload becomes "The B-52's"), keeping version words like (Clean) or (Remix) from the upload
    # an upload whose title isn't "Artist - Title" ("Wagon Wheel Darius Rucker Lyrics Video." from a lyrics
    # channel) comes out as channel + whole title: when the song was asked for by name, the name asked for
    # (spelled the catalog's way when it knows it) is used instead
    want = getattr(args, "_want", None) if getattr(args, "_from_search", False) else None
    if want and want[0]:
        wa, wt = CATALOG_NAME if CATALOG_NAME else want
        sq0 = lambda x: re.sub(r"[^a-z0-9]+", "", fold(x or ""))
        if sq0(primary_artist(wa)) not in sq0(artist) or sq0(re.sub(r"\s*[\(\[].*$", "", wt)) not in sq0(title):
            keep = re.findall(r"\((?:[^)]*(?:clean|dirty|remix|edit|intro|acapella|instrumental)[^)]*)\)", title, re.I)
            artist, title = wa, wt + "".join(" " + k for k in keep if k.lower() not in wt.lower())
    if getattr(args, "_from_search", False) and CATALOG_NAME:
        ca, ct = CATALOG_NAME
        sq = lambda x: re.sub(r"[^a-z0-9]+", "", fold(re.sub(r"\s*[\(\[].*$", "", x or "")))
        if sq(ca) == sq(artist) or sq(primary_artist(ca)) == sq(primary_artist(artist)):
            artist = ca if sq(ca) == sq(artist) else artist.replace(primary_artist(artist), primary_artist(ca), 1)
        if sq(ct) == sq(title):
            title = ct + title[len(re.sub(r"\s*[\(\[].*$", "", title)):]
    base = safe_filename(f"{artist} - {title}")
    genre_code = None
    lib = None if getattr(args, "plain", False) else library_root(args.inbox)   # shared app: no sorting
    guessed = False
    new_folder = None
    genre_code = None
    if lib:
        genre_code = pick_genre(artist, title, info)
        new_folder = holiday_folder(title)        # holiday songs go to Holidays/<holiday> first...
        if new_folder and genre_code and GENRE_PATH.get(genre_code, "").startswith("Hip Hop"):
            new_folder = None                     # ...but not a rap song that only names a holiday
        if new_folder:                            # (XXXTENTACION - A Ghetto Christmas Carol stays in Rap)
            genre_code = None
        if not genre_code and not new_folder:
            emit("progress", url=url, stage="Finding the genre", pct=78)
            genre_code = lookup_genre(artist, title)
            if genre_code:
                _remember(artist, genre_code)
        if not genre_code and not new_folder:
            # no folder for this kind of song yet: make a specific one for it (your library only)
            new_folder = new_genre_folder(artist, title)
            if new_folder:
                existing = [f for f in GENRE_PATH.values() if f.lower() == new_folder.lower()]
                new_folder = existing[0] if existing else new_folder
                emit("info", url=url, message=f"New genre folder: {new_folder}")
        if not genre_code and not new_folder:
            genre_code, guessed = "PP", True       # every song gets a folder; Pop is the safest guess
        outdir = lib / (new_folder or GENRE_PATH[genre_code])
        outdir.mkdir(parents=True, exist_ok=True)
    elif getattr(args, "sort", "off") != "off":
        # everyone else's copy, with sorting on: genre folders inside THEIR chosen folder. Only songs
        # DJ Grab downloads are sorted; nothing they already had is moved.
        genre_code = pick_genre(artist, title, info)
        if not genre_code:
            emit("progress", url=url, stage="Finding the genre", pct=78)
            genre_code = lookup_genre(artist, title)
        rel = shared_folder(genre_code, args.sort, Path(args.inbox))
        if rel:
            outdir = Path(args.inbox) / rel
            outdir.mkdir(parents=True, exist_ok=True)
    dst = outdir / f"{base}.mp3"
    have = existing_file(outdir, base) or (find_in_library(lib, base) if lib else None) or existing_file(Path(args.inbox), base)
    if have and not args.overwrite:
        emit("skipped", url=url, path=str(have), message=f"Already in library: {have.name}")
        return
    emit("progress", url=url, stage="Converting", pct=80)
    tmp_dst = dst.with_name(dst.stem + ".part.mp3")
    stream = probe_audio(src_path)
    q = to_mp3(src_path, tmp_dst, stream)
    if not q["source_kbps"]:
        q["source_kbps"] = round(info.get("abr") or info.get("tbr") or 0)

    emit("progress", url=url, stage="Checking loudness", pct=88)
    # Always measured: a badly distorted upload is thrown away rather than saved.
    lufs, peak = measure_loudness(tmp_dst)
    if peak is not None and peak > HOT_PEAK:
        try:
            tmp_dst.unlink()
        except Exception:
            pass
        raise HotUpload(peak)
    warnings = [] if args.no_loudness else loudness_warnings(lufs, peak)
    cut = high_cutoff(tmp_dst)

    # album art: the real album/single cover from the music catalog. A YouTube video frame is never used;
    # with no official art found the song simply has none (better than a screenshot)
    cover = Path(tempfile.mkdtemp()) / "cover.jpg"
    emit("progress", url=url, stage="Getting the album art", pct=93)
    if not official_cover(artist, title, cover):
        cover = None

    year = info.get("release_year") or (info.get("upload_date") or "")[:4] or None
    loud_txt = ""
    if lufs is not None:
        loud_txt = f" | {lufs:.1f} LUFS, peak {peak:+.1f} dBTP" if peak is not None else f" | {lufs:.1f} LUFS"
    # plain words for the quality: Opus/AAC sound better than MP3 at the same number
    k, codec = q["source_kbps"] or 0, (q["source_codec"] or "").lower()
    eff = k * (1.6 if codec in ("opus", "vorbis") else 1.3 if codec in ("aac", "mp4a") else 1.0)
    if q["lossless_source"]:
        quality = "Best quality"
    elif eff >= 180:
        quality = "High quality"
    elif eff >= 120:
        quality = "Good quality"
    elif k:
        quality = "Low quality"
    else:
        quality = "Quality unknown"
    chan = info.get("channel") or info.get("uploader") or ""
    # Minimum quality (Settings): the stream's real quality AND how high its sound goes (a "high quality"
    # stream made from a muffled copy has nothing above ~12-14 kHz). Below it: a search tries the next
    # upload; when none is good enough, or for a link you pasted, DJ Grab asks before saving it.
    min_k = getattr(args, "min_kbps", 0) or 0
    low = []
    if min_k and not q["lossless_source"] and k and eff < min_k:
        low.append(f"low quality stream (about {int(eff)} kbps)")
    if min_k and cut is not None and cut <= 14000:
        low.append(f"sounds muffled (nothing above {cut / 1000:.0f} kHz)")
        quality = "Low quality"
    if low and not getattr(args, "allow_low", False):
        reason = "Poor quality: " + " and ".join(low) + (f", from {chan}" if chan else "")
        try:
            tmp_dst.unlink()
        except Exception:
            pass
        LOW_SEEN.append((info.get("webpage_url") or url, eff * (1 if cut is None else min(1, cut / 16000)), reason))
        if getattr(args, "_from_search", False):
            raise LowQualityUpload(reason)
        emit("lowquality", url=url, link=info.get("webpage_url") or url, name=f"{artist} - {title}", message=reason)
        return
    qnum = " (lossless)" if q["lossless_source"] else (f" ({int(k)} kbps)" if k else "")   # the download's own bitrate
    src_txt = quality + qnum + (f" · from {chan} on YouTube" if chan else "")
    meta = {
        "artist": artist, "title": title,
        "album": info.get("album") or "",
        "year": year,
        "genre_code": genre_code,
        "genre": GENRE_NAME.get(genre_code) or (new_folder.split("/")[-1] if new_folder else "") or info.get("genre") or (info.get("genres") or [None])[0] or "",
        "url": info.get("webpage_url") or url,
        "source_note": f"Source: {info.get('extractor_key', '')} {src_txt}{loud_txt}".strip(),
    }
    emit("progress", url=url, stage="Tagging and saving", pct=95)
    write_tags(tmp_dst, meta, cover)
    tmp_dst.replace(dst)
    if quality == "Low quality":
        warnings.append(f"Low quality ({src_txt}). Look for a better upload if you can")
    playlist_crate = None
    if lib and info.get("_djgrab_playlist"):
        inbox_root = volume_root(args.inbox)
        if inbox_root:
            playlist_crate = crate_name_for(Path(args.inbox), inbox_root) + "%%" + safe_filename(info["_djgrab_playlist"])
    crate = None if getattr(args, "plain", False) else queue_for_crate(dst, playlist_crate)
    crate_status = None
    if crate:
        added, waiting = flush_crates()
        crate_status = "waiting" if waiting else "added"
    emit("done", url=url, path=str(dst), artist=artist, title=title,
         lufs=lufs, peak=peak, source=src_txt, warnings=warnings,
         crate=crate.replace("%%", " / ") if crate else None, crate_status=crate_status,
         genre=(GENRE_PATH.get(genre_code) + (" (genre guessed)" if guessed else "")) if genre_code else (new_folder + " (new folder)" if new_folder else None))


def grab(url, args):
    import yt_dlp
    if url.startswith("search:"):
        query = url[len("search:"):].strip()
        a, t = split_query(query)
        a, t, fixed = correct_query(a, t)
        args._want = (a, t)          # the song you asked for, so an upload's messy title can't rename it
        if fixed:
            emit("progress", url=url, stage=f"Fixed spelling: {t} by {a}", pct=3, name=f"{t} - {a}")
            query = f"{a} - {t}"
        lib = library_root(Path(os.path.expanduser(args.out)))
        similar = library_matches(lib or Path(os.path.expanduser(args.out)), a, t) if t else []
        want = version_sig(t)
        have = [h for h in similar if version_sig(file_title(h)) == want]   # same version only
        others = [h for h in similar if h not in have]
        if args.check:
            emit("checked", url=url, name=query, status="have" if have else "new", matches=have,
                 message=("Already in your folders: " + ", ".join(have)) if have else "New, not in your folders")
            return
        if have:
            emit("skipped", url=url, path=str((lib or Path(args.out)) / have[0]),
                 message="Already in your folders: " + ", ".join(Path(h).name for h in have))
            return
        if others:   # a different version is in your folders; grab this one too
            emit("match", url=url, status="have", matches=others)
        emit("progress", url=url, stage="Searching YouTube", pct=5, name=query)
        found, reason = search_song(query)
        if not found:
            emit("error", url=url, message=reason)
            return
        LOW_SEEN.clear()
        for n, (cand_url, label) in enumerate(found, 1):
            emit("info", url=url, message="Found: " + label)
            emit("progress", url=url, stage="Found it, getting audio" if n == 1 else f"Trying upload {n} of {len(found)}", pct=15)
            if _grab_url(cand_url, args, single=True) != "hot":
                return
        # every upload was below your minimum quality: ask instead of saving it (the app shows a popup);
        # the best of them is offered, never a distorted one
        best = max(LOW_SEEN, key=lambda x: x[1]) if LOW_SEEN else None
        if best:
            emit("lowquality", url=url, link=best[0], name=query, message=best[2])
        else:
            emit("error", url=url, message="Not downloaded: every upload found was distorted. Paste a YouTube link to grab one anyway.")
        return
    _grab_url(url, args)


def _grab_url(url, args, single=False):
    import yt_dlp
    args._from_search = single
    outdir_root = Path(os.path.expanduser(args.out))
    check_drive(outdir_root)
    outdir_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="djgrab-"))
    state = {"url": url}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                frac = d.get("downloaded_bytes", 0) / total
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.5
            now = time.time()
            if now - state.get("last", 0) > 0.4 or frac >= 1:
                state["last"] = now
                emit("progress", url=url, stage="Downloading", pct=round(20 + min(frac, 1) * 55, 1),
                     name=state.get("name", ""))

    base_opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "ffmpeg_location": str(Path(FFMPEG).parent),
        "socket_timeout": 30, "retries": 5,
    }
    if args.cookies_browser:
        base_opts["cookiesfrombrowser"] = (args.cookies_browser,)

    # 1) Look at the link first so playlists/sets get their own folder.
    with yt_dlp.YoutubeDL({**base_opts, "extract_flat": "in_playlist", "skip_download": True,
                           "noplaylist": True}) as y:  # a song link with &list= stays one song
        top = y.extract_info(url, download=False)
    entries = [top]
    outdir = outdir_root
    args.inbox = str(outdir_root)
    if top.get("_type") == "playlist":
        entries = [e for e in (top.get("entries") or []) if e]
        folder = safe_filename(top.get("title") or "Playlist")
        if not library_root(outdir_root):          # sorting off: playlists get their own folder
            outdir = outdir_root / folder
            outdir.mkdir(parents=True, exist_ok=True)
        emit("info", url=url, message=f"Playlist '{top.get('title')}' with {len(entries)} tracks -> {outdir}",
             count=len(entries), folder=str(outdir))

    opts = {
        **base_opts,
        # Best audio-only stream (YouTube: Opus ~160k; SoundCloud: original file when the
        # uploader allows downloads, otherwise the best stream). Falls back to a muxed file.
        "format": "bestaudio/best",
        "outtmpl": str(work / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "progress_hooks": [hook],
        "noplaylist": True,
    }
    for i, e in enumerate(entries, 1):
        item_url = e.get("webpage_url") or e.get("url") or url
        state["name"] = e.get("title") or item_url
        emit("item_start", url=url, item_url=item_url, index=i, total=len(entries), name=state["name"])
        emit("progress", url=url, stage="Getting track info", pct=18)
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(item_url, download=False)
                a, t = parse_names(info)
                b = safe_filename(f"{a} - {t}")
                lib = library_root(outdir_root)
                look_in = lib or outdir_root
                matches = library_matches(look_in, a, t)
                status = "have" if matches else "new"
                if args.check:
                    emit("checked", url=url, name=f"{a} - {t}", status=status, matches=matches,
                         message=("Already in your folders: " + ", ".join(matches)) if matches else "New, not in your folders")
                    continue
                emit("match", url=url, status=status, matches=matches)
                have = existing_file(outdir, b) or (find_in_library(lib, b) if lib else None)
                if have and not args.overwrite:
                    emit("skipped", url=url, path=str(have), message=f"Already in library: {have.name}")
                    continue
                info = y.process_ie_result(info, download=True)
            media = Path(info["requested_downloads"][0]["filepath"])
            thumb = None
            for th in info.get("thumbnails") or []:
                if th.get("filepath") and Path(th["filepath"]).exists():
                    thumb = Path(th["filepath"])
            if not thumb:
                cands = [p for p in work.glob(f"{info['id']}.*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
                thumb = cands[0] if cands else None
            if top.get("_type") == "playlist":
                info["_djgrab_playlist"] = top.get("title") or "Playlist"
                if not info.get("album"):
                    info["album"] = top.get("title")
            process_entry(info, media, thumb, outdir, url, args)
        except HotUpload as ex:
            if single:
                for p in work.glob("*"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                shutil.rmtree(work, ignore_errors=True)
                return "hot"
            emit("error", url=url, item_url=item_url,
                 message=f"Not downloaded: {ex}. Look for a better upload if you need it.")
        except Exception as ex:
            emit("error", url=url, item_url=item_url, message=friendly_error(str(ex).replace("\x1b[0;31mERROR:\x1b[0m ", "")))
        finally:
            for p in work.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
    shutil.rmtree(work, ignore_errors=True)


def update_ytdlp():
    r = run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"])
    emit("info", message="yt-dlp updated" if r.returncode == 0 else "yt-dlp update failed: " + r.stderr[-200:])


def be_polite():
    """Keep the computer usable while DJ Grab works. DJGRAB_SPEED (Settings > While working):
      balanced (default): lower priority and half the processor cores, so other apps stay smooth
      background: lowest priority (macOS runs it on the efficiency cores), slowest but barely noticeable
      full: as fast as possible"""
    speed = os.environ.get("DJGRAB_SPEED", "balanced")
    if speed == "full":
        return speed
    n = max(2, (os.cpu_count() or 4) // 2) if speed == "balanced" else 2
    for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
              "OPENBLAS_NUM_THREADS"):
        os.environ[v] = str(n)              # read when the audio / AI libraries start
    try:
        if sys.platform == "win32":
            import ctypes
            cls = 0x00000040 if speed == "background" else 0x00004000   # IDLE / BELOW_NORMAL priority
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), cls)
        else:
            os.nice(19 if speed == "background" else 10)
            if speed == "background" and sys.platform == "darwin":
                os.setpriority(4, 0, 0x1000)    # PRIO_DARWIN_PROCESS, PRIO_DARWIN_BG: efficiency cores, low disk priority
    except Exception:
        pass
    return speed


def main():
    global JSON_MODE, FFMPEG, FFPROBE
    be_polite()
    ap = argparse.ArgumentParser(description="Download the best audio from a link as a tagged MP3 320.")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--out", default="/Volumes/Vinny's USB/Vinny's USB/00 DJ GRAB NEW")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-loudness", action="store_true")
    ap.add_argument("--sort", choices=["off", "simple", "detailed"], default="off",
                    help="shared app: genre folders inside the chosen folder")
    ap.add_argument("--normalize", type=float, default=0.0,
                    help="target loudness in LUFS (e.g. -10); 0 = leave the volume as it is")
    ap.add_argument("--min-kbps", type=int, default=0, help="minimum quality; below it DJ Grab asks first")
    ap.add_argument("--allow-low", action="store_true", help="save it even if it's below the minimum quality")
    ap.add_argument("--plain", action="store_true",
                    help="the shared app: save to the chosen folder only (no genre sorting, no DJ software crates)")
    ap.add_argument("--cookies-browser", choices=["chrome", "safari", "firefox", "brave", "edge"])
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--check", action="store_true", help="only check whether the songs are already in your folders")
    ap.add_argument("--fix-names", action="store_true",
                    help="fill blank Serato Name/Artist fields from the songs' tags (only while Serato is closed)")
    ap.add_argument("--flush-crates", action="store_true",
                    help="add waiting tracks to their Serato crates (only while Serato is closed)")
    args = ap.parse_args()
    global NORMALIZE
    NORMALIZE = args.normalize
    JSON_MODE = args.json
    if args.update:
        update_ytdlp()
        return
    if args.fix_names:
        with _CrateLock():
            r = apply_renames()
            if r:
                emit("renamed", count=r, message=f"Cleaned up the names of {r} songs")
            for vol in (VOLUMES.iterdir() if VOLUMES.is_dir() else []):
                if (vol / "_Serato_").is_dir() and os.path.ismount(vol):
                    try:
                        purge_serato_refs(vol, "_To Delete/")
                        # DJ Grab intros whose file is gone (Serato's own library keeps writing them back)
                        purge_serato_refs(vol, None, pred=lambda p, v=vol: p.endswith(" (Intro).mp3")
                                          and not (v / p).exists())
                    except Exception as ex:
                        emit("info", message=f"Couldn't tidy Serato: {ex}")
            n = fix_serato_names()
            try:
                a_n = audit_genres()
                if a_n:
                    emit("info", message=f"Genre check: looked up {a_n} more songs")
            except Exception as ex:
                emit("info", message=f"Genre check paused: {ex}")
            try:
                g = restore_grids()
                if g:
                    emit("info", message=f"Put back DJ Grab's beatgrid on {g} intro versions")
            except Exception as ex:
                emit("info", message=f"Couldn't check intro grids: {ex}")
        emit("names", fixed=n, message=f"Fixed the Name and Artist of {n} tracks in Serato")
        return
    if args.flush_crates:
        added, waiting = flush_crates()
        emit("crates", added=added, waiting=waiting,
             message=f"{added} added to Serato crates, {waiting} waiting for Serato to close")
        return
    if not args.urls:
        ap.error("give at least one link")
    FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")
    for u in args.urls:
        u = u.strip()
        if not re.match(r"https?://|search:", u):
            emit("error", url=u, message="That doesn't look like a link")
            continue
        if re.search(r"(open|play)\.spotify\.com|spotify\.link|^spotify:", u):
            emit("error", url=u, message="Spotify links can't be downloaded, since Spotify keeps its music locked "
                 "to its own app. Type or paste the song name instead (like Title - Artist) and DJ Grab will find it.")
            continue
        if re.search(r"music\.apple\.com|tidal\.com|deezer\.com/.*/(track|album|playlist)", u):
            emit("error", url=u, message="Links from music streaming apps can't be downloaded. Type or paste "
                 "the song name instead (like Title - Artist) and DJ Grab will find it.")
            continue
        try:
            grab(u, args)
        except Exception as ex:
            emit("error", url=u, message=friendly_error(ex))
    emit("finished")


if __name__ == "__main__":
    main()
