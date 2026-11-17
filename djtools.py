#!/usr/bin/env python3
"""DJ Grab — Intros & Cues.

For each song: separate stems (Demucs), find the structure on Serato's beatgrid, then
  * write cue points into the file (1 Start, 2 Vocals, 3 Drop, 4 Breakdown, 5 Outro;
    existing cues that have a name move to the end, unnamed ones are replaced)
  * build "<Artist> - <Title> (Intro).mp3": 16 bars of drums, 16 bars of the beat, then the song.

  djtools.py batch --root "/Volumes/Vinny's USB/Vinny's USB" [--bars 32] [--json]
  djtools.py one FILE [--bars 32] [--json]
Progress is saved after every song, so a batch can be stopped and started again anytime.
"""
import argparse, base64, json, os, re, shutil, struct, subprocess, sys, tempfile, time, unicodedata, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import djgrab  # noqa: E402  (shared helpers: crates, Serato check, ffmpeg)
if __name__ == "__main__":
    djgrab.be_polite()            # before numpy and the AI libraries load, so the thread limit takes

STATE = djgrab.APP_SUPPORT / "intros_state.json"
SR = 44100
# Cues on every song: Start, Chorus, Breakdown, Outro (intro versions also get Halfway). No Chorus 2 or
# Vocals (you asked): old ones get cleared on the next run because the names stay on the "ours" list.
OUR_CUES = [("Start", 0x00CC00), ("Drop", 0xCC0000), ("Breakdown", 0xCCCC00), ("Outro", 0x8800CC)]
LAST_SLOT = 7            # Serato's 8th cue stays empty for the DJ
CUE_LABEL = {"Drop": "Chorus", "Drop 2": "Chorus 2"}
OUR_NAMES = {"Chorus", "Chorus 2", "Build"} | {n for n, _ in OUR_CUES} | {f"Bar {k}" for k in range(1, 1000, 8)} | {"Halfway", "Vocals", "Intro", "Beat In", "Melody In", "Filter Open", "Hook", "Song Start", "Song", "Drop 2"}
SKIP_WORDS = re.compile(r"\b(intro|acap+ella|acapela|instrumental|inst|clean intro|dirty intro|edit intro|"
                        r"extended|outro|transition|effect|sfx|scratch)\b", re.I)
SKIP_FOLDERS = ("Tools", "Other", "00 DJ GRAB NEW", "Mixes")


def emit(event, **d):
    if JSON:
        print(json.dumps({"event": event, **d}), flush=True)
    else:
        print(event, d.get("message") or d.get("name") or "", d.get("pct", ""), flush=True)


LOG = HERE / "logs" / "intros.log"


def log(msg):
    """Plain-text run log next to the engine (Documents/DJ Grab/logs), kept under ~1 MB."""
    try:
        LOG.parent.mkdir(exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix(".old.log"))
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except Exception:
        pass


def log_exc(where):
    import traceback
    log(where + ":\n" + traceback.format_exc())


JSON = False
RETEMPO = True                 # stretch songs a hair off a whole BPM to exactly that BPM (originals and intros)
MAKE_INTRO = True


# ------------------------------------------------------------------ Serato tags
def read_beatgrid(id3):
    for f in id3.getall("GEOB"):
        if f.desc == "Serato BeatGrid":
            d = f.data
            n = struct.unpack(">I", d[2:6])[0]
            if n < 1:
                return None
            first = struct.unpack(">f", d[6:10])[0]
            if n == 1:
                bpm = struct.unpack(">f", d[10:14])[0]
            else:  # variable grid: use the first section's tempo
                beats = struct.unpack(">I", d[10:14])[0]
                nxt = struct.unpack(">f", d[14:18])[0]
                bpm = 60.0 * beats / max(1e-6, nxt - first)
            return first, bpm
    return None




def parse_markers2(data):
    """-> list of (name, payload) entries, in order."""
    if not data or len(data) < 3:
        return []
    b = data[2:]
    if b"\x00" in b:
        b = b[:b.index(b"\x00")]
    b = b.replace(b"\n", b"")
    try:
        dec = base64.b64decode(b + b"=" * ((-len(b)) % 4))
    except Exception:
        dec = base64.b64decode(b[:-1] + b"=" * ((-len(b[:-1])) % 4))
    out, i = [], 2
    while i < len(dec) and dec[i] != 0:
        j = dec.index(b"\x00", i)
        name = dec[i:j].decode("latin1")
        ln = struct.unpack(">I", dec[j + 1:j + 5])[0]
        out.append((name, dec[j + 5:j + 5 + ln]))
        i = j + 5 + ln
    return out


def markers2_bytes(entries):
    body = b"\x01\x01" + b"".join(n.encode("latin1") + b"\x00" + struct.pack(">I", len(p)) + p for n, p in entries) + b"\x00"
    body += b"\x00" * ((-len(body)) % 3)                # no "=" padding in the base64, like Serato
    b64 = base64.b64encode(body)
    b64 = b"\n".join(b64[i:i + 72] for i in range(0, len(b64), 72))
    out = b"\x01\x01" + b64
    return out + b"\x00" * max(1, 470 - len(out))


def cue_payload(slot, ms, rgb, name):
    return (b"\x00" + bytes([slot]) + struct.pack(">I", max(0, int(ms))) + b"\x00" +
            rgb.to_bytes(3, "big") + b"\x00\x00" + name.encode("utf-8") + b"\x00")


def read_cue(p):
    return {"slot": p[1], "ms": struct.unpack(">I", p[2:6])[0], "rgb": int.from_bytes(p[7:10], "big"),
            "name": p[12:-1].decode("utf-8", "replace")}


CUES_V2 = djgrab.APP_SUPPORT / "cues_v2_done"
# Songs you said shouldn't have an intro version (the "No intro" button, or by hand): matched by
# artist + title, so a moved or renamed file still counts. They still get cues and a beatgrid.
NO_INTRO = djgrab.APP_SUPPORT / "no_intro.json"
NO_INTRO_START = ["neil diamond - sweet caroline"]


def _song_key(p):
    s = unicodedata.normalize("NFC", Path(p).stem).lower()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)
    return re.sub(r"\s+", " ", s).strip()


FORCE_INTRO = djgrab.APP_SUPPORT / "force_intro.json"     # "Make intro" on a song DJ Grab thought didn't need one


def force_intro(p):
    try:
        return _song_key(p) in set(json.loads(FORCE_INTRO.read_text()))
    except Exception:
        return False


def no_intro(p):
    try:
        keys = set(json.loads(NO_INTRO.read_text()))
    except Exception:
        keys = set()
    return _song_key(p) in keys | set(NO_INTRO_START)

# ------------------------------------------------------------------ time left
# How long each step takes on this Mac, learned from every song (averaged, recent songs count more).
# Stem separation is learned per minute of song. The app shows the time left from these.
STEP_FILE = djgrab.APP_SUPPORT / "step_times.json"
STEP_DEFAULT = {"tempo check": 5.0, "separation": 6.0, "beat grid": 0.5, "write cues": 0.3,
                "intro build": 32.0, "encode intro": 4.5}
PER_MINUTE = {"separation"}


def _steps():
    try:
        return {**STEP_DEFAULT, **json.loads(STEP_FILE.read_text())}
    except Exception:
        return dict(STEP_DEFAULT)


def send_eta(steps, mins):
    d = _steps()
    sec = sum(d.get(s, 0.0) * (mins if s in PER_MINUTE else 1.0) for s in steps)
    emit("eta", seconds=round(sec, 1), done_at=round(time.time() + sec, 1))


def learn_steps(T, mins):
    d = _steps()
    for k, v in T.items():
        if k in STEP_DEFAULT and v > 0.05:
            x = v / mins if k in PER_MINUTE and mins > 0 else v
            d[k] = round(0.7 * d[k] + 0.3 * x, 2)
    try:
        STEP_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def tidy_cues(root):
    """One-time update of cues already on your songs: Chorus 2 and Song Start come off, the intro's middle
    cue is called Halfway, and the rest move up so the pads have no gaps. Only DJ Grab's own cues change;
    yours stay. Runs once, while Serato is closed."""
    from mutagen.id3 import ID3, GEOB
    changed = 0
    files = [f for f in Path(root).rglob("*.mp3") if not f.name.startswith("._") and "_To Delete" not in f.parts]
    for n, f in enumerate(files):
        if n % 200 == 0:
            emit("progress", stage=f"Updating cue names on your songs (one time): {n} of {len(files)}",
                 pct=round(100 * n / max(1, len(files))))
        try:
            id3 = ID3(str(f))
            old = next((g.data for g in id3.getall("GEOB") if g.desc == "Serato Markers2"), b"")
            entries = parse_markers2(old)
            cues = sorted((read_cue(p) for nm, p in entries if nm == "CUE"), key=lambda c: c["slot"])
            if not any(c["name"] in ("Chorus 2", "Song Start", "Build") for c in cues):
                continue
            keep = [c for c in cues if c["name"] not in ("Chorus 2", "Song Start")]
            for c in keep:
                if c["name"] == "Build":
                    c["name"] = "Halfway"
            new = [("CUE", cue_payload(i, c["ms"], c["rgb"], c["name"])) for i, c in enumerate(keep[:LAST_SLOT])]
            others = [(nm, p) for nm, p in entries if nm != "CUE"]
            color = [e for e in others if e[0] == "COLOR"] or [("COLOR", b"\x00\xff\xff\xff")]
            rest = [e for e in others if e[0] != "COLOR"]
            id3.delall("GEOB:Serato Markers2")
            id3.delall("GEOB:Serato Markers_")
            id3.add(GEOB(encoding=0, mime="application/octet-stream", desc="Serato Markers2",
                         data=markers2_bytes(color + new + rest)))
            id3.save(str(f), v2_version=id3.version[1] if id3.version[1] in (3, 4) else 3)
            changed += 1
        except Exception:
            continue
    log(f"cue tidy: {changed} songs updated in {root}")
    return changed


GIVE_WAY = ["Chorus 2", "Drop 2", "Breakdown", "Vocals"]     # dropped first when the pads run out


def phrase_cues(bar_times, first, times, names, already=()):
    """The song's parts (drop, breakdown...) placed on its 8-bar system: counted on the grid from the bar at
    index `first` (the first note), each part goes on the nearest 8-bar line, not where the music analysis
    happened to land. A point that's already taken is not placed twice."""
    out = []
    taken = [t for _, t, _ in already]
    base = float(bar_times[first])
    for n, rgb in names:
        v = times.get(n)
        if v is None:
            continue
        k = int(np.argmin(np.abs(np.asarray(bar_times[first:], float) - v)))     # its bar, counted from the first note
        j = first + int(round(k / 8.0)) * 8                                       # nearest 8-bar line
        if j >= len(bar_times):
            continue
        t = float(bar_times[j]) * 1000
        if all(abs(t - u) > 50 for u in taken):
            out.append((CUE_LABEL.get(n, n), t, rgb)); taken.append(t)
    return out


def first_sound(orig, until):
    """Seconds to the song's first real sound before `until`: the first 10 ms where the level comes within
    40 dB of the song's loudest and stays there (most of the next 200 ms), so a click, a tag blip or
    encoder noise in the silence before the song never counts. None if nothing before `until`."""
    n = int(max(0.0, until) * SR)
    if n <= 0:
        return None
    h = int(0.01 * SR)
    m = orig[:n].mean(axis=1) if orig.ndim == 2 else orig[:n]
    k = len(m) // h
    if k == 0:
        return None
    lv = np.sqrt((m[:k * h].reshape(k, h) ** 2).mean(axis=1))
    thr = 10 ** (-40 / 20) * (float(np.abs(orig).max()) + 1e-9)
    for i in np.nonzero(lv > thr)[0]:
        if float(np.median(lv[i:i + 20])) > thr:
            return max(0.0, i * h / SR - 0.01)
    return None


def pads(cues):
    """(name, ms, rgb) -> (slot, name, ms, rgb): in song order with no gaps, top row (1-4) then the bottom
    row (5-7). Pad 8 stays empty, so when there are too many, Drop 2 gives way first."""
    cues = sorted(cues, key=lambda c: c[1])
    for n in GIVE_WAY:
        if len(cues) <= LAST_SLOT:
            break
        cues = [c for c in cues if c[0] != n]
    return [(i, n, ms, rgb) for i, (n, ms, rgb) in enumerate(cues[:LAST_SLOT])]


def fix_half_beat(bars_t, g, orig, bpm, name):
    """The beat tracker can lock onto the off-beats (in house the open hi-hat on the "and" is often louder
    than the kick), which puts every beat - and the start of every intro - half a beat late. The kick is on
    the beat: if the low end hits clearly harder half way between the beats than on them, the whole grid
    moves half a beat earlier."""
    try:
        pbt = 60.0 / float(bpm)
        mono = orig.mean(axis=1) if orig.ndim == 2 else orig
        end = min(len(mono), int((float(bars_t[min(64, len(bars_t) - 1)]) + 1) * SR))
        lo = _lowpass(mono[:end, None], 120.0)[:, 0]
        hop = int(0.005 * SR)
        env = np.sqrt(np.convolve(lo ** 2, np.ones(hop) / hop, "same"))[::hop]
        on = np.maximum(0.0, np.diff(env, prepend=env[0]))
        beats = np.concatenate([b + pbt * np.arange(4) for b in np.asarray(bars_t, float)[:64]])
        def score(off):
            idx = ((beats + off) / 0.005).astype(int)
            idx = idx[(idx >= 4) & (idx < len(on) - 4)]
            return float(np.mean([on[i - 4:i + 5].max() for i in idx])) if len(idx) else 0.0
        s_on, s_half = score(0.0), score(pbt / 2)
        if s_half > 1.6 * s_on and s_half > 0:
            nb_ = np.asarray(bars_t, float) - pbt / 2
            if nb_[0] < -0.02:
                nb_ = nb_ + 4 * pbt
                nb_ = np.concatenate([[nb_[0] - 4 * pbt], nb_]) if nb_[0] - 4 * pbt >= -0.02 else nb_
            log(f"{name}: grid was on the off-beats (kick {s_on:.4f} on the beat vs {s_half:.4f} between), moved half a beat")
            m = [(max(0.0, p - pbt / 2), n, b) for p, n, b in g.get("markers") or []]
            return nb_, dict(g, bars=nb_, markers=m or [(float(max(0.0, nb_[0])), None, bpm)])
    except Exception:
        log_exc(f"{name}: half-beat check")
    return bars_t, g


def place_downbeats(bars_t, g, sig, orig, bpm, name):
    """Beat 1 on the song's first note (where the Start cue goes), when that note sits on a beat: the
    whole grid keeps its tempo and beats and only its bar count moves (1, 2 or 3 beats)."""
    pbt = 60.0 / bpm
    b0 = float(bars_t[0])
    pk = np.abs(orig[:int((b0 + 0.1) * SR)]).max(axis=1) if b0 > 0 else np.zeros(0)
    thr = 10 ** (-40 / 20) * (float(np.abs(orig).max()) + 1e-9)
    nz = np.nonzero(pk > thr)[0]
    if not len(nz):
        return bars_t, g
    fn = nz[0] / SR
    k = int(round((b0 - fn) / pbt))
    if k <= 0 or abs((b0 - k * pbt) - fn) > 0.08:
        return bars_t, g
    k = k % 4
    if k == 0:
        return bars_t, g
    nb_ = np.asarray(bars_t, float) - k * pbt
    # only when the song agrees: a first note can be a pickup ("Love Shack", "Up All Night" open a few
    # beats before the 1). The real 1 of each bar usually has the kick / bass hit, so the move is kept only
    # if the new bar lines land on clearly stronger low hits than the old ones over the first 32 bars
    try:
        mono = orig.mean(axis=1) if orig.ndim == 2 else orig
        mono = mono[:int((float(np.asarray(bars_t)[min(32, len(bars_t) - 1)]) + 2) * SR)]
        lo = _lowpass(mono[:, None], 150.0)[:, 0] if mono.ndim == 1 else _lowpass(mono, 150.0)
        hop = int(0.01 * SR)
        env = np.sqrt(np.convolve(lo ** 2, np.ones(hop) / hop, "same"))[::hop]
        on = np.maximum(0.0, np.diff(env, prepend=env[0]))
        def score(lines):
            idx = (np.asarray(lines[:32]) * 100).astype(int)
            idx = idx[(idx >= 3) & (idx < len(on) - 3)]
            return float(np.mean([on[i - 3:i + 4].max() for i in idx])) if len(idx) else 0.0
        old_s, new_s = score(np.asarray(bars_t, float)), score(nb_)
        if new_s < 1.15 * old_s:
            log(f"{name}: first note is a pickup (low hits on the old bar lines {old_s:.4f} vs {new_s:.4f}), beat 1 kept")
            return bars_t, g
    except Exception:
        log_exc(f"{name}: downbeat check")
    while nb_[0] - 4 * pbt >= -0.08:
        nb_ = np.concatenate([[nb_[0] - 4 * pbt], nb_])
    nb_ = nb_[nb_ >= -0.08]
    log(f"{name}: beat 1 moved {k} beat{'s' if k > 1 else ''} earlier, onto the song's first note")
    return nb_, dict(g, bars=nb_, markers=[(float(max(0.0, nb_[0])), None, bpm)])


def follow_band(bars, sig, bpm):
    """Live bands drift: lay the grid on where each 8-bar stretch really lands. Every 8 bars the downbeat is
    moved onto the song's own hits (searched within 40 ms, judged on the whole stretch, not one hit); bars
    in between are spaced evenly. A quiet or unclear stretch keeps the steady grid. Returns new bar times
    and how far it moved them."""
    fps = SR / HOP
    s = np.convolve(sig, np.ones(3) / 3, "same")
    bars = np.asarray(bars, float)
    if len(bars) < 9:
        return bars, 0.0
    p = 60.0 / float(bpm)
    idx = list(range(0, len(bars), 8))
    shifts, strength = [], []
    for k in idx:
        b = bars[k] + p * np.arange(32)
        b = b[b < len(s) / fps - 0.1]
        if len(b) < 8:
            shifts.append(0.0); strength.append(0.0); continue
        f = lambda sh: float(_interp(s, (b + sh - ONSET_BIAS) * fps).mean())
        cand = np.arange(-0.04, 0.0401, 0.002)
        sc = [f(x) for x in cand]
        j = int(np.argmax(sc))
        shifts.append(float(cand[j]) if sc[j] > 1.02 * f(0.0) else 0.0)
        strength.append(sc[j])
    strength = np.array(strength)
    ok = strength >= 0.4 * float(np.median(strength[strength > 0])) if np.any(strength > 0) else strength > 0
    sh = np.array([shifts[i] if ok[i] else 0.0 for i in range(len(idx))])
    sm = sh
    out = bars + np.interp(np.arange(len(bars)), idx, sm)   # each bar nudged; odd bars stay odd
    return out, float(np.max(np.abs(sm)))


def grid_every8(path, grid, cues):
    """Grid markers every 8 bars through the whole song (each one on the beat the grid already puts there),
    (Serato loads to the Start cue, which sits on the first note even when that's before the first downbeat)."""
    if len(grid) > 1 and all((m[1] or 32) % 32 == 0 for m in grid):
        pass                             # resampled below to one marker every 8 bars
    try:
        from mutagen.mp3 import MP3
        dur = float(MP3(path).info.length)
    except Exception:
        return grid
    beats = marker_beats(grid, dur)
    if len(beats) < 2:
        return grid
    spb = float(np.median(np.diff(beats)))
    bpm = float(grid[-1][2] or 60.0 / spb)
    idx = list(range(0, len(beats), 32))
    out = [(float(beats[i]), 32, None) for i in idx[:-1]]
    out.append((float(beats[idx[-1]]), None, bpm))
    return out


def _other_app(path, cues, bpm, first):
    """Rekordbox, Traktor or VirtualDJ: hand the same cues to djcues (no-op for Serato)."""
    try:
        import djcues
        t = a = ""
        try:
            from mutagen.easyid3 import EasyID3
            e = EasyID3(str(path))
            t, a = (e.get("title") or [""])[0], (e.get("artist") or [""])[0]
        except Exception:
            pass
        djcues.remember(str(path), cues, bpm, first, t, a)
    except Exception as e:
        print("cue export:", e)


def write_cues(path, cues, grid=None):
    """cues: list of (slot 0-6, name, ms, rgb), normally from pads() (song order, no gaps), in decoder
    time; they (and the grid) are moved onto Serato's MP3 timeline here.
    Named cues already in the file (not ours) move to the free slots after ours; unnamed ones are replaced.
    The last slot (8 in Serato) is always left empty for your own cue.
    Returns the names written, in slot order."""
    shift = serato_offset(path)
    cues = [(sl, name, ms + shift * 1000, rgb) for sl, name, ms, rgb in cues]
    if grid:
        if isinstance(grid, tuple):                   # (first downbeat, bpm)
            grid = [(grid[0], None, grid[1])]
        grid = grid_every8(path, grid, cues)
        grid = [(pos + shift, nxt, bpm) for pos, nxt, bpm in grid]
    from mutagen.id3 import ID3, GEOB
    final = path
    path = os.path.join(os.path.dirname(final), "." + os.path.basename(final) + ".djgrab-tmp")
    shutil.copy2(final, path)              # edit a copy; the original is only replaced once the copy is complete
    id3 = ID3(path)
    old = next((f.data for f in id3.getall("GEOB") if f.desc == "Serato Markers2"), b"")
    entries = parse_markers2(old)
    others = [(n, p) for n, p in entries if n != "CUE"]
    kept = sorted((read_cue(p) for n, p in entries if n == "CUE"), key=lambda c: c["slot"])
    kept = [c for c in kept if c["name"].strip() and c["name"].strip() not in OUR_NAMES]
    ours = [c for c in sorted(cues) if c[0] < LAST_SLOT]
    kept = kept[:LAST_SLOT]
    while len(ours) + len(kept) > LAST_SLOT and ours:  # make room for your named cues
        drop = next((c for n in GIVE_WAY for c in ours if c[1] == n), ours[-1])
        ours.remove(drop)
    # ours fill the pads from 1 with no gaps, your named cues right after; pad 8 stays empty
    placed = {i: (name, ms, rgb) for i, (_, name, ms, rgb) in enumerate(ours)}
    for i, c in enumerate(kept, start=len(ours)):
        placed[i] = (c["name"], c["ms"], c["rgb"])
    new = [("CUE", cue_payload(sl, ms, rgb, name)) for sl, (name, ms, rgb) in sorted(placed.items())]
    color = [e for e in others if e[0] == "COLOR"] or [("COLOR", b"\x00\xff\xff\xff")]
    rest = [e for e in others if e[0] != "COLOR"]
    data = markers2_bytes(color + new + rest)
    id3.delall("GEOB:Serato Markers2")
    id3.delall("GEOB:Serato Markers_")   # old-format copy: Serato rewrites it from Markers2
    if grid:                             # (first downbeat seconds, bpm): Serato's grid lines match the cues
        id3.delall("GEOB:Serato BeatGrid")
        id3.add(GEOB(encoding=0, mime="application/octet-stream", desc="Serato BeatGrid", data=beatgrid_markers_bytes(grid)))
    id3.add(GEOB(encoding=0, mime="application/octet-stream", desc="Serato Markers2", data=data))
    id3.save(path, v2_version=3 if id3.version[1] == 3 else 4)
    os.replace(path, final)
    return [placed[sl][0] for sl in sorted(placed)]


# ------------------------------------------------------------------ audio
def decode(path, out_wav):
    r = subprocess.run([djgrab.FFMPEG, "-y", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
                        "-c:a", "pcm_s16le", str(out_wav)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("couldn't decode: " + r.stderr[-200:])


def load_wav(p):
    import numpy as np
    with wave.open(str(p)) as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2" if sw == 2 else "<i4").astype("float32")
    a /= 32768.0 if sw == 2 else 2147483648.0
    return a.reshape(-1, ch)


def save_wav(p, a):
    import numpy as np
    a = np.clip(a, -1, 1)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(a.shape[1]); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((a * 32767).astype("<i2").tobytes())


def ensure_demucs():
    try:
        import demucs  # noqa: F401
        import soundfile  # noqa: F401
        import numpy  # noqa: F401
        return
    except Exception:
        pass
    emit("progress", stage="One-time setup: downloading the stem separator (about 1 GB)", pct=2)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "demucs==4.0.1", "soundfile", "numpy", "torch<2.9", "torchaudio<2.9"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("couldn't install the stem separator: " + r.stderr[-300:])
    global np
    import numpy as np  # noqa: F811


_MODEL = None


def _demucs_model():
    """Load the stem model once and keep it for every song in the run (saves ~10-20 s a song)."""
    global _MODEL
    if _MODEL is None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        import torch
        from demucs.pretrained import get_model
        dev = ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
        m = get_model("htdemucs")
        m.eval()
        _MODEL = (m, dev)
    return _MODEL


def _separate_cli(wav, outdir):
    env = dict(os.environ, PYTORCH_ENABLE_MPS_FALLBACK="1")
    dev = "cpu"
    try:
        import torch
        if torch.backends.mps.is_available():
            dev = "mps"
        elif torch.cuda.is_available():          # Windows PCs with an NVIDIA card
            dev = "cuda"
    except Exception:
        pass
    r = subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs", "-d", dev, "-o", str(outdir),
                        "--filename", "{stem}.{ext}", str(wav)], capture_output=True, text=True, env=env)
    if r.returncode:
        raise RuntimeError("stem separation failed: " + r.stderr[-300:])
    base = Path(outdir) / "htdemucs"
    return {s: load_wav(base / f"{s}.wav") for s in ("drums", "bass", "other", "vocals")}


# ------------------------------------------------------------------ studio-quality stems for the intro
MODEL_DIR = djgrab.APP_SUPPORT / "models"
DRUMS_FT_URL = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/f7e0c4bc-ba3fe64a.th"
_HQ_FAILED = None
_DRUMS_FT = None


def ensure_hq():
    """BS-RoFormer (vocals/instrumental) + Demucs' fine-tuned drums model. One-time ~720 MB download.
    Returns True when ready; on any failure the run carries on with the normal stems."""
    global _HQ_FAILED
    if _HQ_FAILED:
        return False
    try:
        try:
            import beartype, einops, rotary_embedding_torch  # noqa: F401
        except Exception:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "beartype<0.19", "einops",
                                "rotary-embedding-torch", "packaging"], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stderr[-200:])
        import hqsep
        if not hqsep.model_path(MODEL_DIR).exists():
            emit("progress", stage="One-time setup: downloading the studio stem model (640 MB)", pct=3)
            hqsep.download(MODEL_DIR, lambda f: emit("progress", stage=f"Downloading studio stem model {int(f * 100)}%",
                                                      pct=3) if int(f * 1000) % 50 == 0 else None)
        return True
    except Exception as ex:
        log_exc("studio stem setup")
        _HQ_FAILED = str(ex)[:120]
        emit("progress", stage=f"Studio stems unavailable, using standard ({_HQ_FAILED[:50]})", pct=5)
        return False


def _drums_ft():
    """Demucs htdemucs_ft's drums specialist only (~80 MB, cached by torch)."""
    global _DRUMS_FT
    if _DRUMS_FT is None:
        import torch
        from demucs.states import load_model
        pkg = torch.hub.load_state_dict_from_url(DRUMS_FT_URL, map_location="cpu", check_hash=True)
        m = load_model(pkg)
        m.eval()
        dev = ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
        _DRUMS_FT = (m, dev)
    return _DRUMS_FT


def _drums_from(audio):
    import torch
    from demucs.apply import apply_model
    m, dev = _drums_ft()
    x = torch.from_numpy(np.ascontiguousarray(audio[:, :2].T)).float()
    ref = x.mean(0)
    mu, sd = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        try:
            out = apply_model(m, ((x - mu) / sd)[None], device=dev, shifts=2, split=True, overlap=0.5,
                              progress=False)[0]
        except Exception:
            out = apply_model(m, ((x - mu) / sd)[None], device="cpu", shifts=2, split=True, overlap=0.5,
                              progress=False)[0]
    out = (out * sd + mu).cpu().numpy()
    return np.ascontiguousarray(out[m.sources.index("drums")].T.astype("float32"))


HQ_SPANS = {}            # studio stems separated this run, per song: (start, end, inst, drums) in samples


def _hq_keep(key, span):
    if key not in HQ_SPANS:
        HQ_SPANS.clear()                    # only the song being worked on stays in memory
    HQ_SPANS.setdefault(key, []).append(span)


def _hq_within(key, a, b):
    """Studio stems for samples a..b cut out of a longer stretch already separated for the same song (this
    run, or saved in the cache by an earlier run), or None. Only separations that finished are ever saved,
    so a failed one is never reused."""
    spans = [s for s in HQ_SPANS.get(key, []) if s[0] <= a and b <= s[1]]
    if spans:
        s = min(spans, key=lambda s: s[1] - s[0])
        return s[2][a - s[0]:b - s[0]], s[3][a - s[0]:b - s[0]]
    if CACHE is None:
        return None
    for f in (CACHE / "songs").glob(f"{key}-hq-*-*.npz"):
        try:
            a0, b0 = (int(v) for v in f.stem.rsplit("-", 2)[1:])
        except ValueError:
            continue
        if a0 <= a and b <= b0:
            ck = cache_get(f.stem)
            if ck is not None and len(ck["inst"]) == b0 - a0 and len(ck["drums"]) >= b0 - a0:
                inst_c, drums_c = ck["inst"].astype("float32"), ck["drums"].astype("float32")
                _hq_keep(key, (a0, b0, inst_c, drums_c))
                return inst_c[a - a0:b - a0], drums_c[a - a0:b - a0]
    return None


def hq_stems(orig, t0, t1):
    """Clean instrumental and drums for orig[t0:t1] (seconds), as full-length arrays that are zero outside
    that stretch, or None if the studio models can't run. Separated with 2 s of context either side so the
    edges are as clean as the middle."""
    if not ensure_hq():
        return None
    try:
        import hqsep
        a = max(0, int((t0 - 2.0) * SR))
        b = min(len(orig), int((t1 + 2.0) * SR))
        clip = orig[a:b, :2] if orig.shape[1] >= 2 else np.repeat(orig[a:b, :1], 2, axis=1)
        key = audio_key(orig)
        name = f"{key}-hq-{a}-{b}"
        ck = cache_get(name)
        wide = None if ck is not None and len(ck["inst"]) == b - a else _hq_within(key, a, b)
        if ck is not None and len(ck["inst"]) == b - a:
            inst_c, drums_c = ck["inst"].astype("float32"), ck["drums"].astype("float32")
        elif wide is not None:
            # this stretch was already separated as part of a longer one (an intro rebuilt for a later entry
            # point): cut it out of that instead of separating again. The longer one had at least as much
            # context either side, so the cut is just as clean
            inst_c, drums_c = wide
        else:
            voc = hqsep.vocals(clip, MODEL_DIR)
            DEVICES["roformer"] = hqsep._MODEL[1] if hqsep._MODEL else "?"
            inst_c = clip - voc
            drums_c = _drums_from(inst_c)
            cache_put(name, inst=inst_c, drums=drums_c)
            _hq_keep(key, (a, b, inst_c, drums_c))
        inst = np.zeros((len(orig), 2), "float32")
        drums = np.zeros((len(orig), 2), "float32")
        inst[a:b] = inst_c
        drums[a:b] = drums_c[:b - a]
        if orig.shape[1] == 1:
            inst, drums = inst[:, :1], drums[:, :1]
        return {"inst": inst, "drums": drums}
    except Exception as ex:
        log_exc("studio stems")
        emit("progress", stage=f"Studio stems failed, using standard ({str(ex)[:50]})", pct=50)
        return None


# ------------------------------------------------------------------ cache on the USB (max 10 GB)
CACHE_LIMIT = 10e9
DEVICES = {}            # where each model actually ran (logged per song)
PREFETCH = {}           # next song decoded in the background while this one runs
GENRE_TEMPOS = []       # tempos common in the current song's genre folder (learned as batches run)
CACHE = None            # <drive>/_DJ Grab Cache, set per run
HOPL = 1024             # level-envelope frame (23 ms)


def use_usb_storage(path):
    """Keep DJ Grab's heavy files on the drive the songs live on, not the Mac: the analysis cache, the
    studio stem model (moved off the Mac if it is still there) and the downloaded model weights."""
    global CACHE, MODEL_DIR
    vol = djgrab.volume_root(str(path))
    if vol is None:
        return
    base = Path(vol) / "_DJ Grab Cache"
    if CACHE == base:
        return
    try:
        (base / "songs").mkdir(parents=True, exist_ok=True)
        (base / "models").mkdir(exist_ok=True)
        (base / "torch").mkdir(exist_ok=True)
    except OSError:
        return
    CACHE = base
    old = djgrab.APP_SUPPORT / "models"
    try:
        for f in old.glob("*.ckpt"):
            if not (base / "models" / f.name).exists():
                shutil.move(str(f), str(base / "models" / f.name))        # frees ~640 MB on the Mac
    except Exception:
        log_exc("moving the stem model to the USB")
    MODEL_DIR = base / "models"
    os.environ["TORCH_HOME"] = str(base / "torch")                       # Demucs / Beat This! weights
    try:
        import torch.hub
        torch.hub.set_dir(str(base / "torch" / "hub"))
    except Exception:
        pass


CUR_PATH = None
_INDEX = None


def _index():
    global _INDEX
    if _INDEX is None:
        try:
            _INDEX = json.loads((CACHE / "index.json").read_text())
        except Exception:
            _INDEX = {}
    return _INDEX


def _touch(name):
    if CACHE is None:
        return
    key = name.split("-")[0]
    e = _index().setdefault(key, {})
    e["used"] = time.time()
    if CUR_PATH:
        e["path"] = CUR_PATH


def save_index():
    if CACHE is None or _INDEX is None:
        return
    try:
        tmp = CACHE / "index.json.tmp"
        tmp.write_text(json.dumps(_INDEX)); tmp.replace(CACHE / "index.json")
    except Exception:
        pass


def cache_clean(force=False):
    """Daily tidy of the USB cache. Removes: half-finished downloads and temp files; cache for songs that
    are gone; old versions of a song whose audio changed (tempo fix, re-download); studio stems not used
    for 30 days and analysis not used for 120 days. The 10 GB cap still applies on top."""
    if CACHE is None:
        return
    stamp = CACHE / ".last_clean"
    if not force and stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
        return
    removed, freed = 0, 0

    def rm(f):
        nonlocal removed, freed
        try:
            freed += f.stat().st_size
            f.unlink()
            removed += 1
        except OSError:
            pass
    for f in list((CACHE / "torch").rglob("*.partial")) + list((CACHE / "songs").glob(".*.tmp.npz")):
        rm(f)
    idx = _index()
    vol = CACHE.parent
    latest = {}
    for key, e in idx.items():
        pth = e.get("path")
        if pth and (latest.get(pth) is None or e.get("used", 0) > idx[latest[pth]].get("used", 0)):
            latest[pth] = key
    now = time.time()
    drop = set()
    for key, e in idx.items():
        pth = e.get("path")
        if pth and not Path(pth).exists() and Path(pth).is_relative_to(vol):
            drop.add(key)                                     # the song is gone
        elif pth and latest.get(pth) != key:
            drop.add(key)                                     # an older version of the song's audio
    for f in (CACHE / "songs").glob("*.npz"):
        key = f.name.split("-")[0]
        e = idx.get(key)
        age = now - (e.get("used", 0) if e else f.stat().st_mtime)
        if key in drop or (e is None and age > 86400) or age > (30 if "-hq-" in f.name else 120) * 86400:
            rm(f)
    for key in drop:
        idx.pop(key, None)
    save_index()
    stamp.touch()
    if removed:
        log(f"cache tidy: removed {removed} files, freed {freed / 1e6:.0f} MB")


def audio_key(audio):
    import hashlib
    x = np.ascontiguousarray(audio[::97]).astype("float16").tobytes()
    return hashlib.sha1(x + str(len(audio)).encode()).hexdigest()[:20]


FRESH = os.environ.get("DJGRAB_FRESH") == "1"     # "Redo intro": separate the song again, nothing from the cache


def cache_get(name):
    if CACHE is None or FRESH:
        return None
    f = CACHE / "songs" / (name + ".npz")
    try:
        with np.load(f, allow_pickle=False) as z:
            d = {k: z[k] for k in z.files}
        os.utime(f)                                   # recently used = kept longest
        _touch(name)
        return d
    except Exception:
        return None


def cache_put(name, **arrays):
    if CACHE is None:
        return
    f = CACHE / "songs" / (name + ".npz")
    try:
        tmp = f.with_name("." + f.name + ".tmp.npz")
        np.savez_compressed(tmp, **{k: (v.astype("float16") if hasattr(v, "dtype") and v.dtype.kind == "f"
                                       and v.size > 4096 else v) for k, v in arrays.items()})
        os.replace(tmp, f)
        _touch(name)
        cache_prune()
    except Exception:
        log_exc("cache write")


_PRUNE_T = 0.0


def cache_prune():
    """Keep the song cache under 10 GB: least recently used entries go first."""
    global _PRUNE_T
    if CACHE is None or time.time() - _PRUNE_T < 60:
        return
    _PRUNE_T = time.time()
    files = [(f.stat().st_mtime, f.stat().st_size, f) for f in (CACHE / "songs").glob("*.npz")]
    total = sum(sz for _, sz, _ in files)
    if total <= CACHE_LIMIT:
        return
    for _, sz, f in sorted(files):
        f.unlink(missing_ok=True)
        total -= sz
        if total <= 0.9 * CACHE_LIMIT:
            break


def stem_levels(stems):
    """Mean-square envelopes (23 ms frames) of the mixes the analysis looks at."""
    def ms(x):
        m = x.mean(axis=1) if x.ndim == 2 else x
        n = len(m) // HOPL
        return (m[:n * HOPL].reshape(n, HOPL) ** 2).mean(axis=1).astype("float32")
    beat = stems["drums"] + stems["bass"] + stems["other"]
    return {"vocals": ms(stems["vocals"]), "drums": ms(stems["drums"]), "beat": ms(beat),
            "every": ms(beat + stems["vocals"])}


class LiteStems:
    """Cached analysis in place of the full stems; the real stems are only separated if something needs
    them (a fallback path), then kept for the rest of the song."""
    def __init__(self, lev, loader):
        self.lev, self._load, self._s = lev, loader, None

    def __getitem__(self, k):
        if self._s is None:
            self._s = self._load()
        return self._s[k]


def genre_profile_update(path, bpm, style):
    """What's typical per genre folder (tempos, how intros came out), kept on the USB with the cache."""
    if CACHE is None:
        return
    f = CACHE / "genre_profiles.json"
    try:
        prof = json.loads(f.read_text()) if f.exists() else {}
        g = genre_of(path) or "?"
        e = prof.setdefault(g, {"n": 0, "bpm": [], "styles": {}})
        e["n"] += 1
        e["bpm"] = (e["bpm"] + [round(float(bpm), 2)])[-300:]
        key = style.split(",")[0] if style else "no intro"
        e["styles"][key] = e["styles"].get(key, 0) + 1
        tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(prof)); tmp.replace(f)
    except Exception:
        pass


def genre_tempos(path):
    try:
        prof = json.loads((CACHE / "genre_profiles.json").read_text())
        b = prof.get(genre_of(path) or "?", {}).get("bpm", [])
        vals, counts = np.unique(np.round(b), return_counts=True)
        return [float(v) for v, c in zip(vals, counts) if c >= 3]
    except Exception:
        return []


def separate(wav, outdir, audio=None):
    """-> dict of stem name -> array (drums, bass, other, vocals).
    Runs in this process with the model kept loaded, straight from memory (no stem files
    written to disk). Falls back to the command-line tool if that fails."""
    try:
        import numpy as np
        import torch
        from demucs.apply import apply_model
        m, dev = _demucs_model()
        a = audio if audio is not None else load_wav(wav)
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        x = torch.from_numpy(np.ascontiguousarray(a[:, :2].T)).float()
        ref = x.mean(0)
        mu, sd = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            out = apply_model(m, ((x - mu) / sd)[None], device=dev, shifts=1, split=True,
                              overlap=0.25, progress=False)[0]
        out = (out * sd + mu).cpu().numpy()
        stems = {name: np.ascontiguousarray(out[i].T) for i, name in enumerate(m.sources)}
        if all(k in stems for k in ("drums", "bass", "other", "vocals")):
            DEVICES["demucs"] = dev
            return stems
    except Exception as ex:
        emit("progress", stage=f"Using the slower stem splitter ({str(ex)[:60]})", pct=20)
        log_exc("in-app stem separation")
    DEVICES["demucs"] = "separate tool (slow)"
    return _separate_cli(wav, outdir)



# ------------------------------------------------------------------ Serato's MP3 timeline
ONSET_BIAS = 0.0208     # our kick detector lands this much before the actual hit (measured on a click track)




def serato_offset(path):
    """Seconds Serato's timeline runs behind a normal decoder (ffmpeg) for this MP3. Serato plays the
    Xing/Info header frame and the encoder's start padding as audio; ffmpeg skips both. Measured on
    Vinny's library: about 39 ms on files with that header, 0 without."""
    try:
        d = open(path, "rb").read(400000)
    except OSError:
        return 0.0
    off = 0
    if d[:3] == b"ID3":
        sz = (d[6] << 21) | (d[7] << 14) | (d[8] << 7) | d[9]
        off = 10 + sz + (10 if d[5] & 0x10 else 0)
    while off < len(d) - 4 and not (d[off] == 0xFF and (d[off + 1] & 0xE0) == 0xE0):
        off += 1
    if off >= len(d) - 4:
        return 0.0
    h = d[off + 1:off + 4]
    version = (h[0] >> 3) & 3                     # 3 = MPEG1
    sr_idx = (h[1] >> 2) & 3
    rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
    sr = rates.get(version, (44100,))[sr_idx if sr_idx < 3 else 0]
    spf = 1152 if version == 3 else 576
    fr = d[off:off + 2000]
    for tag in (b"Xing", b"Info"):
        i = fr.find(tag, 0, 200)
        if i < 0:
            continue
        pad = 0
        enc = fr[i + 120:i + 129]
        if enc[:4] in (b"LAME", b"Lavc", b"Lavf", b"L3.9"):
            pad = ((fr[i + 141] << 4) | (fr[i + 142] >> 4)) if len(fr) > i + 143 else 0
        return (spf + pad) / sr
    return 0.0

# ------------------------------------------------------------------ beats (our own, not Serato's grid)
try:
    import numpy as np  # noqa: E402
except ImportError:      # installed on first run by ensure_demucs()
    np = None
HOP = 256
ENGINE = "@30"
INTRO_RULE = 17              # no tension bar or filter sweep; clash check lines the song up first; repairs only when clean


def _is_dance(p):
    parts = Path(p).parts
    return (len(parts) >= 3 and parts[-3] in LONG_INTRO_TOPS) or (len(parts) >= 2 and parts[-2] in LONG_INTRO_SUBS)


# What each intro-rule update changed, and which finished songs it can change. A plain Start re-checks only
# those songs (made under an older rule and affected) and skips everything else that's already done.
RULE_CHANGES = {
    6: lambda p, kind: kind == "intro" and _is_dance(p),     # house / dance: many intros no longer needed
    7: lambda p, kind: kind == "intro",                      # every intro DJ Grab made: start / handover timing
    8: lambda p, kind: kind == "intro",                      # clean start on the attack, no drum fills that clash
    9: lambda p, kind: kind == "intro",                      # every intro 8 bars; bar lines on the hit's attack
    10: lambda p, kind: Path(p).name in HALF_BEAT_MOVED,     # grids wrongly moved half a beat: put back
    11: lambda p, kind: Path(p).name in HALF_BEAT_MOVED,     # song start moved too far / intro wrongly left out
    12: lambda p, kind: kind == "intro",                     # intro versions get a Song cue
    13: lambda p, kind: Path(p).name in BEAT_JUMPED,         # intros whose beat jumped mid-intro: built again
    14: lambda p, kind: Path(p).name in BEAT_JUMPED,         # beat 1 cut off (hit ahead of the bar line): built again
    15: lambda p, kind: Path(p).name in NO_CLEAN_INTRO,      # left without an intro after 2 tries: 3 tries, best kept and flagged
    16: lambda p, kind: Path(p).name in _flagged_before(),   # drums-clash check misread songs a few ms off: checked again
    17: lambda p, kind: kind == "intro",                     # no snare-roll / riser build, no filter sweep: every intro again
}
BEAT_JUMPED = {"Blackstreet - No Diggity - Uncensored.mp3", "Blackstreet feat. Dr. Dre & Queen Pen - No Diggity (Clean).mp3",
               "ROSE & Bruno Mars - APT. (Clean).mp3", "Chris Lorenzo & Kah-Lo - In This Bih'.mp3"}
_FLAGGED = None


def _flagged_before():
    """Songs flagged up to the clash-check fix (17 Nov 2026, 18:30)."""
    global _FLAGGED
    if _FLAGGED is None:
        _FLAGGED = set()
        try:
            for line in open(HERE / "logs" / "flagged_intros.jsonl"):
                r = json.loads(line)
                if r.get("when", "") < "2026-11-17 18:30":
                    _FLAGGED.add(r["song"])
        except Exception:
            pass
    return _FLAGGED


NO_CLEAN_INTRO = {"17 - MK.mp3", "A Guy Called Gerald - Voodoo Ray.mp3", "After Hours - Waterfalls (4am Mix).mp3",
                  "Bee Gees - Stayin' Alive.mp3", "Before I Let Go - Frankie Beverly.mp3",
                  "Bill Withers - Just The Two Of Us (Tall Boys love nwantiti (ah ah ah) Edit).mp3",
                  "Blackstreet - No Diggity - Uncensored.mp3", "Bobby Brown - Every Little Step I Take.mp3",
                  "Brick & Lace - Love Is Wicked.mp3", "Turn On The Lights - Fred Again.mp3"}
HALF_BEAT_MOVED = {"DJ Deeon - Let Me Bang.mp3", "Get Busy With It - Conducta.mp3", "SECRETS NOTION w Cameron Hayes.mp3",
                   "Turn On The Lights - Fred Again.mp3",
                   "Michael Bibi. feat. Uniiqu3 - Bad Wolf (Original Mix).mp3",
                   "Parris Mitchell feat. DJ Funk, Parris Mitchell, DJ Funk - Follow Me Ghetto (Acid).mp3"}


def needs_recheck(st, k):
    """A finished song that a newer rule could change (so a plain Start runs it again)."""
    made = st.get("rule", {}).get(k, 0)
    kind = st.get("kind", {}).get(k)
    return any(v > made and f(k, kind) for v, f in RULE_CHANGES.items())
GRID_RULE = 26                # bumped whenever what DJ Grab writes into songs changes (grid, cues, comment,
                             # name labels, intro rules): every finished song gets one quick recheck
                             # 2: edited-song grids  3: no Vocals cue, comments, (Acap In/Out), no tension on soft entries  4: era with accents  5: no name labels on originals  6: carried beat must fit the opening  7: skip a quiet opening nothing fits over (pool style)  8: pool-style build (full low end, 4-bar loop)            # results from older versions (Serato-grid cues) get redone


def onset_env(a):
    """Spectral-flux onset strength (frames of HOP samples)."""
    x = a.mean(axis=1) if a.ndim == 2 else a
    n = 1024
    win = np.hanning(n).astype("float32")
    nfr = 1 + (len(x) - n) // HOP
    if nfr < 10:
        return np.zeros(1), np.zeros(1)
    idx = np.arange(n)[None, :] + HOP * np.arange(nfr)[:, None]
    flux = np.empty(nfr, "float32"); low = np.empty(nfr, "float32")
    prev = None
    for s in range(0, nfr, 2048):
        fr = x[idx[s:s + 2048]] * win
        mag = np.log1p(100 * np.abs(np.fft.rfft(fr, axis=1)))
        if prev is None:
            prev = mag[:1]
        d = np.diff(np.vstack([prev, mag]), axis=0)
        flux[s:s + len(fr)] = np.maximum(d, 0).sum(axis=1)
        low[s:s + len(fr)] = np.maximum(d[:, 1:12], 0).sum(axis=1)   # ~40-500 Hz: kicks
        prev = mag[-1:]
    flux -= np.convolve(flux, np.ones(32) / 32, "same"); flux = np.maximum(flux, 0)
    low -= np.convolve(low, np.ones(32) / 32, "same"); low = np.maximum(low, 0)
    return flux, low

def _interp(env, t):
    i = np.clip(t, 0, len(env) - 1.001)
    i0 = i.astype(int); f = i - i0
    return env[i0] * (1 - f) + env[i0 + 1] * f

def tempo(env, hint=None):
    fps = SR / HOP
    e = env - env.mean()
    ac = np.correlate(e[: int(fps * 120)], e[: int(fps * 120)], "full")[len(e[: int(fps * 120)]) - 1:]
    ac /= ac[0] + 1e-9
    best = None
    for bpm in np.arange(70, 180.01, 0.25):
        lag = 60 * fps / bpm
        sc = sum(w * _interp(ac, np.array([lag * k]))[0] for k, w in ((1, 1.0), (2, 0.6), (4, 0.4), (0.5, 0.2)))
        sc *= np.exp(-0.5 * (np.log2(bpm / 118) / 0.9) ** 2)
        if hint and min(abs(bpm - h) / h for h in (hint, hint * 2, hint / 2)) < 0.02:
            sc *= 1.15
        if best is None or sc > best[0]:
            best = (sc, bpm)
    return best[1]

def fit_grid(env, bpm0, span=0.015):
    """Exact period and phase: fold the whole song at candidate periods, keep the sharpest."""
    fps = SR / HOP
    best = None
    for bpm in np.arange(bpm0 * (1 - span), bpm0 * (1 + span), 0.01):
        P = 60 * fps / bpm
        nb = int((len(env) - 1) / P)
        for ph in np.linspace(0, P, 48, endpoint=False):
            t = ph + P * np.arange(nb)
            sc = _interp(env, t).mean()
            if best is None or sc > best[0]:
                best = (sc, bpm, ph)
    sc, bpm, ph = best
    P = 60 * fps / bpm
    # fine phase
    for ph2 in np.linspace(ph - P / 48, ph + P / 48, 21):
        t = ph2 + P * np.arange(int((len(env) - 1) / P))
        s2 = _interp(env, t).mean()
        if s2 > sc: sc, ph = s2, ph2
    return bpm, ph / fps   # seconds of a beat

GENRE_CENTER = {"Hip Hop": 95, "R&B": 95, "Caribbean": 100, "African": 110, "Latin": 100, "Country": 115,
                "Rock": 120, "Pop": 115, "House": 125, "Dance & Electronic": 128, "Club": 140}

def comb(env, bpm):
    fps = SR / HOP
    best = 0
    P = 60 * fps / bpm
    nb = int((len(env) - 1) / P) - 1
    for ph in np.linspace(0, P, 24, endpoint=False):
        best = max(best, _interp(env, ph + P * np.arange(nb)).mean())
    return best / (env.mean() + 1e-9)

def estimate(full, low, hint=None, genre=None):
    if hint and 60 <= hint <= 200:
        return fit_grid(low, hint, span=0.006)
    c = GENRE_CENTER.get(genre, 118)
    cands = set()
    for e in (full, low):
        b = tempo(e)
        for r in (1, 3 / 4, 4 / 3, 2 / 3, 3 / 2, 1 / 2, 2):
            if 65 <= b * r <= 190:
                cands.add(round(b * r, 2))
    raw = {b: comb(full, b) + comb(low, b) for b in cands}
    # every other beat always folds a little sharper; keep the full tempo when it's nearly as strong
    for b in list(raw):
        d = next((o for o in raw if abs(o / b - 2) < 0.02 and o <= 175), None)
        if d and raw[d] >= 0.75 * raw[b]:
            raw.pop(b)
    cands = list(raw)
    top = max(raw.values())
    close = [b for b in cands if raw[b] >= 0.88 * top]          # the genre only breaks near-ties
    best = min(close, key=lambda b: abs(np.log2(b / c)) - 0.25 * raw[b] / top)
    return fit_grid(low, best, span=0.015)


def downbeat(beat_audio, low, bpm, ph):
    """Which of the 4 beats is bar 1: the one where the music changes (sections start on bar 1)
    and the kick hits hardest."""
    beat = 60.0 / bpm
    mono = beat_audio.mean(axis=1)
    nb = int((len(mono) / SR - ph) / beat) - 1
    if nb < 16:
        return ph
    e = np.array([np.sqrt(np.mean(mono[int((ph + k * beat) * SR):int((ph + (k + 1) * beat) * SR)] ** 2)) + 1e-6
                  for k in range(nb)])
    ch = np.abs(np.diff(np.log(e)))                # change going INTO beat k+1
    kick = _interp(low, (ph + beat * np.arange(nb)) * SR / HOP)
    best, bq = -1, 0
    for q in range(4):
        sc = ch[np.arange(q - 1, len(ch), 4)[np.arange(q - 1, len(ch), 4) >= 0]].mean() / (ch.mean() + 1e-9)
        sc += 0.5 * kick[q::4].mean() / (kick.mean() + 1e-9)
        if sc > best:
            best, bq = sc, q
    return ph + bq * beat


def find_grid(stems, hint=None, genre=None, alt=None):
    """-> (first downbeat in seconds, bpm), as the actual hit time on the decoder's timeline.
    alt: Serato's own grid phase (same timeline). If it lines up with the kicks about as well as ours
    (ours sometimes locks onto an off-beat), Serato's is kept."""
    beat_audio = stems["drums"] + stems["bass"] + stems["other"]
    full, _ = onset_env(beat_audio)
    _, low = onset_env(stems["drums"])
    d_rms = float(np.sqrt(np.mean(stems["drums"] ** 2)))
    b_rms = float(np.sqrt(np.mean(beat_audio ** 2))) + 1e-9
    if d_rms < 0.05 * b_rms or low.mean() < 1e-6:     # hardly any drums: follow the whole beat
        low = full
    bpm, ph = estimate(full, low, hint, genre)
    if alt is not None:
        fps = SR / HOP
        beat = 60.0 / bpm
        n = int((len(low) / fps - 1) / beat) - 1

        def score(p):
            p = (p - ONSET_BIAS) % beat
            return float(_interp(low, (p + beat * np.arange(n)) * fps).mean())
        if score(alt) >= 0.9 * score(ph + ONSET_BIAS):
            ph = (alt - ONSET_BIAS) % beat
    return float(downbeat(beat_audio, low, bpm, ph)) + ONSET_BIAS, float(bpm)


# ------------------------------------------------------------------ beat + bar tracking (Beat This!, ISMIR 2024)
_BT = None


def ensure_beat_this():
    try:
        import beat_this  # noqa: F401
        return True
    except Exception:
        pass
    emit("progress", stage="One-time setup: installing the beat tracker", pct=3)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "beat_this==1.1.0", "soxr", "einops",
                        "rotary-embedding-torch", "torch<2.9", "torchaudio<2.9"], capture_output=True, text=True)
    if r.returncode:
        log("beat tracker install failed: " + r.stderr[-800:])
    return r.returncode == 0


def _beat_model():
    global _BT
    if _BT is None:
        import torch
        from beat_this.inference import Audio2Beats
        dev = ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
        try:
            _BT = Audio2Beats(checkpoint_path="final0", device=dev, dbn=False)
        except Exception:
            _BT = Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
    return _BT


def track_beats(audio):
    """Beat and downbeat times (seconds, decoder timeline) from Beat This!, or (None, None)."""
    global _BT
    try:
        if not ensure_beat_this():
            return None, None
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        try:
            beats, downbeats = _beat_model()(audio, SR)
            DEVICES["beats"] = str(getattr(_BT, "device", "?"))
        except Exception:
            from beat_this.inference import Audio2Beats        # the Mac's GPU path failed: use the CPU
            _BT = Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
            beats, downbeats = _BT(audio, SR)
            DEVICES["beats"] = "cpu (graphics chip failed)"
        beats, downbeats = np.asarray(beats, float), np.asarray(downbeats, float)
        if len(beats) < 32:
            return None, None
        return beats, downbeats
    except Exception as ex:
        log_exc("beat tracker")
        emit("progress", stage=f"Beat tracker unavailable, using the backup ({str(ex)[:50]})", pct=66)
        return None, None


def _index_beats(beats):
    """Give every detected beat its beat number (fills gaps where a beat was missed)."""
    ibi = float(np.median(np.diff(beats)))
    k = [0]
    for i in range(1, len(beats)):
        k.append(k[-1] + max(1, int(round((beats[i] - beats[i - 1]) / ibi))))
    return np.array(k, float), ibi


def refine_beats(pos, sig, maxs=0.035):
    """Nudge each beat (up to 35 ms) onto the drum hit nearest it, then smooth over +-4 beats so one loose
    hit can't pull the grid. For songs played live, whose tempo wanders."""
    fps = SR / HOP
    s = np.convolve(sig, np.ones(3) / 3, "same")
    s = s / (np.percentile(s[s > 0], 95) + 1e-9) if np.any(s > 0) else s
    sh = np.arange(-maxs, maxs + 1e-9, 0.002)
    ref, w = np.empty(len(pos)), np.empty(len(pos))
    for i, b in enumerate(pos):
        v = _interp(s, (b + sh - ONSET_BIAS) * fps) - 0.15 * np.abs(sh) / maxs
        j = int(np.argmax(v))
        ref[i], w[i] = b + sh[j], max(float(v[j]), 0.0)
    k = np.arange(len(pos))
    out = np.empty(len(pos))
    for i in range(len(pos)):
        m = np.abs(k - i) <= 4
        kk, bb, ww = k[m], ref[m], w[m] + 1e-3
        for _ in range(2):
            A = np.vstack([np.ones(len(kk)), kk]).T * np.sqrt(ww)[:, None]
            (a, p), *_ = np.linalg.lstsq(A, bb * np.sqrt(ww), rcond=None)
            ww = ww * (np.abs(bb - (a + p * kk)) < 0.02) + 1e-3
        out[i] = a + p * i
    return out


def spliced_grid(grid, p, bpm, signed, strong, s, downbeats, dur, W=16):
    """One exact tempo in pieces: the song was edited and the beat jumps at the cut. Each piece gets its
    own grid marker on its own first downbeat, so every section sits on the beat. None if it isn't that."""
    fps = SR / HOP
    # finer windows than the constant check: short pieces count (16 beats, or 8 for 4-bar inserts)
    signed, strength = [], []
    for k0 in range(0, len(grid) - W, W):
        b = grid[k0:k0 + W]
        f = lambda sh: float(_interp(s, (b + sh - ONSET_BIAS) * fps).mean())
        sh = max(np.arange(-0.07, 0.0701, 0.002), key=f)
        signed.append(sh); strength.append(f(sh))
    signed, strength = np.array(signed), np.array(strength)
    strong = strength >= 0.4 * float(np.median(strength))
    segs = []
    for w in np.where(strong)[0]:
        if segs and abs(signed[w] - segs[-1]["med"]) <= 0.012:
            segs[-1]["ws"].append(int(w))
            segs[-1]["med"] = float(np.median(signed[segs[-1]["ws"]]))
        else:
            segs.append({"ws": [int(w)], "med": float(signed[w])})
    segs = [g for g in segs if len(g["ws"]) >= 2]
    if not segs or len(segs) > 6 or sum(len(g["ws"]) for g in segs) < 0.8 * strong.sum():
        return None
    # where exactly each cut is: the beat that best splits "old offset before, new offset after"
    starts = [0]
    for a_, b_ in zip(segs, segs[1:]):
        lo, hi = (a_["ws"][-1] + 1) * W, b_["ws"][0] * W + W
        hi = min(hi, len(grid))
        va = _interp(s, (grid[lo:hi] + a_["med"] - ONSET_BIAS) * fps)
        vb = _interp(s, (grid[lo:hi] + b_["med"] - ONSET_BIAS) * fps)
        best = max(range(0, hi - lo + 1), key=lambda j: float(va[:j].sum() + vb[j:].sum()))
        starts.append(lo + best)
    starts.append(len(grid))
    bars, seg_first = [], []
    for i, g in enumerate(segs):
        k0, k1 = starts[i], starts[i + 1]
        # within a piece the tempo can be a hair different (pieces from different takes or speeds): follow
        # each piece's own offset trend, measured window by window
        ws = np.array(g["ws"]); tw = grid[np.minimum(ws * W, len(grid) - 1)]
        if len(ws) >= 3:
            b_, a_ = np.polyfit(tw - tw[0], signed[ws], 1)
        else:
            b_, a_ = 0.0, g["med"]
        t = grid[k0:k1] + a_ + b_ * (grid[k0:k1] - tw[0])
        votes = np.zeros(4)
        for d in downbeats:
            if len(t) and t[0] - 0.3 * p <= d <= t[-1] + 0.3 * p:
                k = int(round((d - t[0]) / p))
                if 0 <= k < len(t) and abs(t[k] - d) < 0.3 * p:
                    votes[k % 4] += 1
        kd = int(np.argmax(votes))
        seg_bars = [float(t[k]) for k in range(kd, len(t), 4)]
        if i == 0 and seg_bars:
            first = seg_bars[0]
            while first - 4 * p >= 0:
                first -= 4 * p
            seg_bars = [float(x) for x in np.arange(first, seg_bars[0] - 1e-6, 4 * p)] + seg_bars
        if seg_bars:
            seg_first.append(len(bars))
            bars += seg_bars
    keep = [j for j in range(len(bars)) if 0 <= bars[j] < dur]
    if len(keep) < 16 or len(seg_first) < 2:
        return None
    bars_a = np.array(bars)
    # a marker on every piece's first bar and every 4 bars inside a piece (keeps each beat within a few ms)
    mpos = [j for j in range(len(bars)) if (j - max(f for f in seg_first if f <= j)) % 4 == 0 and bars[j] < dur]
    mk = []
    for n_, j in enumerate(mpos):
        if n_ + 1 < len(mpos):
            j2 = mpos[n_ + 1]
            mk.append((float(bars_a[j]), max(1, int(round((bars_a[j2] - bars_a[j]) / p))), None))
        else:
            mk.append((float(bars_a[j]), None, float(bpm)))
    bars = np.array(sorted(b for b in bars if 0 <= b < dur))
    msg = f"spliced grid: {len(seg_first)} pieces at {', '.join(f'{bars_a[j]:.1f}s' for j in seg_first)}"
    return dict(bars=bars, bpm=bpm, markers=mk, drift=0.012, pieces=len(seg_first), msg=msg)


def repair_inserts(r, s, p, dur):
    """Edited songs (music-video cuts) can hide a short insert, a few bars that sit off from the pieces
    around them, too short to count as a piece. Check every 2 bars; shift a stretch only when its hits
    clearly sit somewhere else: at least 20 ms off, a strong beat there, and twice the fit at the new spot.
    Then grid markers every 2 bars so each stretch sits where it plays."""
    fps = SR / HOP
    bars = np.array(r["bars"], dtype=float)
    if len(bars) < 8:
        return r
    f = lambda b, sh: float(_interp(s, (b + sh - ONSET_BIAS) * fps).mean())
    win = []
    for j in range(0, len(bars) - 2, 2):
        b = np.linspace(bars[j], bars[j + 2], 8, endpoint=False)
        sh = max(np.arange(-0.06, 0.0601, 0.002), key=lambda x: f(b, x))
        win.append((j, sh, f(b, sh), f(b, 0.0)))
    med = float(np.median([w[2] for w in win]))
    fixed = []
    for j, sh, at, on in win:
        if abs(sh) >= 0.02 and at >= 0.5 * med and at >= 2 * on:
            bars[j:j + 2] += sh
            fixed.append(f"{bars[j]:.1f}s ({sh * 1000:+.0f} ms)")
    if not fixed:
        return r
    mk = [(float(bars[j]), max(1, int(round((bars[j + 2] - bars[j]) / p))), None) for j in range(0, len(bars) - 2, 2)]
    mk.append((float(bars[len(mk) * 2]), None, float(r["bpm"])))
    log("grid repair: short inserts at " + ", ".join(fixed))
    return dict(r, bars=bars, markers=mk)


def marker_beats(mk, dur):
    """Every beat time a list of Serato-style grid markers puts down."""
    out = []
    for i, (pos, n, bpm) in enumerate(mk):
        if n:
            out += list(np.linspace(pos, mk[i + 1][0], n, endpoint=False))
        else:
            out += list(pos + 60 / bpm * np.arange(0, max(1, int((dur - pos) * bpm / 60))))
    return np.array(out)


def constant_grid(beats, downbeats, sig, n_samples, hint=None, alts=()):
    """Most produced music is one exact tempo. Find the single tempo + phase that best fits the drum hits
    across the whole song (tracker tempo +-1.5%), then check it holds everywhere there is a beat: every
    32-beat stretch with drums must sit within 15 ms. Talking, skits and breakdowns with no beat are ignored,
    and a missed or doubled beat from the tracker can't bend it. None if the tempo really moves."""
    if sig is None or len(beats) < 32:
        return None
    fps = SR / HOP
    s = np.convolve(sig, np.ones(3) / 3, "same")
    d = np.diff(beats)
    p0 = float(np.median(d))
    d = d[np.abs(d - p0) < 0.08 * p0]
    p0 = float(np.mean(d)) if len(d) else p0          # mean: the tracker's 20 ms steps average out
    dur = n_samples / SR

    def best_phase(p, step):
        bb = np.arange(0.0, dur - 0.5, p)
        ph = np.arange(0.0, p, step)
        tt = ((bb[None, :] + ph[:, None]) - ONSET_BIAS) * fps
        sc = _interp(s, tt).mean(axis=1)
        j = int(np.argmax(sc))
        return float(sc[j]), float(ph[j])

    cands = []
    for bpm in np.arange(60 / p0 * 0.97, 60 / p0 * 1.03, 0.02):
        sc, ph = best_phase(60 / bpm, 0.004)
        cands.append((sc, bpm, ph))
    sc, bpm, ph = max(cands)
    for b2 in np.arange(bpm - 0.03, bpm + 0.0301, 0.002):
        sc2, ph2 = best_phase(60 / b2, 0.002)
        if sc2 > sc:
            sc, bpm, ph = sc2, b2, ph2
    # snap to the tempo the song was made at: most songs are produced at a whole (or half) BPM, and the
    # official tempo online settles which. Only taken if it fits the drum hits as well as the measurement.
    snaps = {round(bpm), round(bpm * 2) / 2} | {t for t in (GENRE_TEMPOS or [])}
    if hint:
        for m in (0.5, 1.0, 2.0):
            snaps.add(float(hint) * m)
    for b2 in sorted(snaps, key=lambda b: abs(b - bpm)):
        if abs(b2 - bpm) > 0.15:
            continue
        sc2, ph2 = best_phase(60 / b2, 0.002)
        log(f"tempo snap test {bpm:.3f} -> {b2:.2f}: fit {sc2 / sc:.3f}")
        if sc2 >= 0.97 * sc:
            bpm, ph, sc = float(b2), ph2, sc2
            break
    p = 60 / bpm
    grid = ph + p * np.arange(0, int((dur - ph) / p) + 1)
    shifts, strength = [], []
    for k0 in range(0, len(grid) - 32, 32):
        b = grid[k0:k0 + 32]
        f = lambda sh: float(_interp(s, (b + sh - ONSET_BIAS) * fps).mean())
        sh = max(np.arange(-0.06, 0.0601, 0.002), key=f)
        shifts.append(sh); strength.append(f(sh))
    if not shifts:
        return None
    strength = np.array(strength); signed = np.array(shifts); shifts = np.abs(signed)
    strong = strength >= 0.4 * float(np.median(strength))
    if strong.sum() < 3:
        return None
    if float(np.mean(shifts[strong] <= 0.015)) < 0.8:
        # not one grid: maybe one tempo with a splice (music-video edits, radio edits) where the beat jumps.
        # the cuts can hide in one signal and show in another (a drum stem that sounds the same across an
        # edit, a full mix that's busy): look for them in each, keep the grid whose beats land best overall
        best = None
        sigs = [None] + [np.convolve(x[:len(s)], np.ones(3) / 3, "same") for x in alts if x is not None and len(x)]
        for sg in sigs:
            if sg is None:
                rs = [spliced_grid(grid, p, bpm, signed, strong, s, downbeats, dur)]
            else:
                if len(sg) < len(s):
                    sg = np.pad(sg, (0, len(s) - len(sg)))
                sh2, st2 = [], []
                for k0 in range(0, len(grid) - 32, 32):
                    b = grid[k0:k0 + 32]
                    f = lambda sh: float(_interp(sg, (b + sh - ONSET_BIAS) * fps).mean())
                    v = max(np.arange(-0.06, 0.0601, 0.002), key=f)
                    sh2.append(v); st2.append(f(v))
                st2 = np.array(st2)
                rs = [spliced_grid(grid, p, bpm, np.array(sh2), st2 >= 0.4 * float(np.median(st2)), sg, downbeats, dur)]
            for r in rs:
                if r is None:
                    continue
                # judged on the combined signal, the same yardstick for every candidate
                score = float(_interp(s, (marker_beats(r["markers"], dur) - ONSET_BIAS) * fps).mean())
                if best is None or score > best[0]:
                    best = (score, r)
        if best:
            log(best[1]["msg"])
            return repair_inserts(best[1], s, p, dur)
        return best[1] if best else None
    votes = np.zeros(4)
    for dbt in downbeats:
        k = int(round((dbt - ph) / p))
        if abs(ph + k * p - dbt) < 0.3 * p:
            votes[k % 4] += 1
    kd = int(np.argmax(votes))
    first = ph + p * kd
    while first - 4 * p >= 0:
        first -= 4 * p
    bars = first + 4 * p * np.arange(0, int((dur - first) / (4 * p)) + 1)
    bars = bars[bars < dur]
    drift = float(np.percentile(shifts[strong], 90))
    return dict(bars=bars, bpm=bpm, markers=[(float(bars[0]), None, bpm)], drift=min(drift, 0.012))


def fit_bars(beats, downbeats, low, n_samples, sig=None):
    """-> dict(bars=array of bar start times, bpm, markers=[(pos, beats_to_next|None, bpm|None)], drift)
    Fits a straight-line grid through every beat (averages out the tracker's 20 ms steps), checks it
    against the kicks, and if the tempo really drifts (live drums), anchors the grid every 8 bars."""
    k, ibi = _index_beats(beats)
    keep = np.ones(len(k), bool)
    for _ in range(3):
        A = np.vstack([np.ones(keep.sum()), k[keep]]).T
        (a, p), *_ = np.linalg.lstsq(A, beats[keep], rcond=None)
        res = beats - (a + p * k)
        keep = np.abs(res) < max(0.04, 3 * np.std(res[keep]))
    # does the tempo drift? mean error over each 32-beat stretch
    drift = 0.0
    for s in range(0, int(k[-1]), 32):
        m = keep & (k >= s) & (k < s + 32)
        if m.sum() >= 8:
            drift = max(drift, abs(float(np.mean(res[m]))))
    fps = SR / HOP
    n_beats = int((n_samples / SR - a) / p) + 1

    def kick_score(shift, pos):
        tt = (pos + shift - ONSET_BIAS) * fps
        tt = tt[(tt > 0) & (tt < len(low) - 2)]
        return float(_interp(low, tt).mean()) if len(tt) else 0.0

    # bar phase: which beat number the downbeats land on most often
    votes = np.zeros(4)
    for d in downbeats:                                # vote with the beat number of the nearest detected beat
        j = int(np.argmin(np.abs(beats - d)))
        if abs(beats[j] - d) < 0.35 * p:
            votes[int(k[j]) % 4] += 1
    kd = int(np.argmax(votes))
    if drift <= 0.012:
        grid = a + p * np.arange(-8, n_beats + 8)
        base = kick_score(0.0, grid)
        best = max(np.arange(-0.025, 0.0251, 0.001), key=lambda sh: kick_score(sh, grid))
        if kick_score(best, grid) > 1.03 * base:
            a += best                                  # nudge onto the actual kicks
        k0 = kd
        while a + p * (k0 - 4) >= 0:
            k0 -= 4
        while a + p * k0 < 0:
            k0 += 4
        bars = a + p * np.arange(k0, n_beats + 4, 4)
        bars = bars[bars < n_samples / SR]
        bpm = 60.0 / p
        return dict(bars=bars, bpm=bpm, markers=[(float(bars[0]), None, bpm)], drift=drift)
    # drifting tempo: smooth local fit around every beat number
    kk = np.arange(int(k[0]) - 8, int(k[-1]) + 8)
    pos = np.empty(len(kk))
    kv, bv = k[keep], beats[keep]
    for i, kn in enumerate(kk):
        m = np.abs(kv - kn) <= 8
        if m.sum() < 4:
            m = np.argsort(np.abs(kv - kn))[:8]
        A = np.vstack([np.ones(len(kv[m])), kv[m]]).T
        (aa, pp), *_ = np.linalg.lstsq(A, bv[m], rcond=None)
        pos[i] = aa + pp * kn
    if sig is not None:
        pos = refine_beats(pos, sig)                   # live drums: pull every beat onto the hits it plays
    first_i = next(i for i in range(len(kk)) if (kk[i] - kd) % 4 == 0 and pos[i] >= 0)
    bar_idx = list(range(first_i, len(kk), 4))
    bars = np.array([pos[i] for i in bar_idx])
    bars = bars[bars < n_samples / SR]
    markers = []
    for j in range(0, len(bars) - 2, 2):                # an anchor every 2 bars (8 beats)
        markers.append((float(bars[j]), 8, None))
    last = bars[len(markers) * 2] if len(markers) * 2 < len(bars) else bars[-1]
    tail_bpm = 60.0 * 4 / float(np.median(np.diff(bars[-9:]))) if len(bars) > 9 else 60.0 / p
    markers.append((float(last), None, tail_bpm))
    return dict(bars=bars, bpm=60.0 / p, markers=markers, drift=drift)


def bars_from_constant(first, bpm, n_samples):
    bar = 4 * 60.0 / bpm
    while first - bar >= 0:
        first -= bar
    while first < 0:
        first += bar
    bars = first + bar * np.arange(int((n_samples / SR - first) / bar) + 1)
    return dict(bars=bars, bpm=bpm, markers=[(float(first), None, bpm)], drift=0.0)


def beatgrid_markers_bytes(markers):
    """Serato BeatGrid with one or more markers: (pos, beats_to_next, None) ... then (pos, None, bpm)."""
    out = b"\x01\x00" + struct.pack(">I", len(markers))
    for pos, nxt, bpm in markers:
        out += struct.pack(">f", pos)
        out += struct.pack(">I", nxt) if nxt is not None else struct.pack(">f", bpm)
    return out + b"\x00"


# ------------------------------------------------------------------ structure on real bars
def phrase_anchor(every, sb, nb, max_pickup=4):
    """The bar the song's 8-bar phrases count from: the first bar of music (sb) unless it is a short pickup
    (up to 4 bars) before a clear hit. Picked by how well each choice puts the song's energy changes on
    phrase lines, and only moved off sb when that is clearly better."""
    db = 20 * np.log10(np.asarray(every) + 1e-9)
    nov = np.abs(np.diff(db, prepend=db[0]))

    def score(a):
        idx = [i for i in range(sb + 1, nb) if (i - a) % 8 == 0]
        return float(nov[idx].mean()) if idx else 0.0

    base = score(sb)
    best = sb
    for a in range(sb + 1, min(sb + max_pickup + 1, nb - 8)):
        jump = db[a] - float(np.mean(db[sb:a]))           # the hit must be clearly louder than the pickup
        if jump >= 8.0 and score(a) > max(1.5 * base, score(best)):
            best = a
    return best


def analyse_bars(stems, bars, n_samples, official=None):
    """Cue times on real bar lines. Section cues sit on 8-bar phrases counted from the song's first bar
    of music (how DJs count); Vocals on the bar where the singing starts."""
    nb = len(bars) - 1
    if nb < 16:
        raise RuntimeError("too short to analyse")
    bt = np.append(bars, n_samples / SR)

    lev = stems.lev if isinstance(stems, LiteStems) else stem_levels(stems)

    def per_bar(k):
        e = lev[k]
        out = np.zeros(nb)
        for i in range(nb):
            a, b = int(bt[i] * SR / HOPL), max(int(bt[i] * SR / HOPL) + 1, int(bt[i + 1] * SR / HOPL))
            seg = e[a:b]
            out[i] = float(np.sqrt(np.mean(seg))) if len(seg) else 0.0
        return out

    voc = per_bar("vocals")
    drm = per_bar("drums")
    beat = per_bar("beat")
    every = per_bar("every")
    blk = lambda x, i, n=8: float(x[max(0, i):max(0, i) + n].mean()) if 0 <= i < nb else 0.0
    t = lambda i: float(bars[i])
    res = {}
    sb = next((i for i in range(nb) if every[i] > 0.1 * np.percentile(every, 90)), 0)
    # music-video talking before the song: bars that are mostly voice with next to no instruments. The song
    # starts at the first two bars where the instruments are really playing.
    b90 = float(np.percentile(beat, 90)) + 1e-9
    m0 = next((i for i in range(sb, nb - 1) if beat[i] > 0.12 * b90 and beat[i + 1] > 0.12 * b90), sb)
    talk = 0
    song_end = None
    if official and n_samples / SR - official >= 8:
        # a music-video rip: the file runs longer than the official song. Find where the song itself sits:
        # the official length's worth of bars that leaves the least music outside it (the talking, sound
        # effects or skits before and after are what's left out)
        extra = n_samples / SR - official
        bar_len = float(np.median(np.diff(bars)))
        cands = [i for i in range(nb) if bars[i] <= extra + 3.0]

        def outside(i):
            st, en = float(bars[i]), float(bars[i]) + official
            return sum(float(beat[j]) for j in range(nb) if bt[j + 1] <= st + 0.5 * bar_len or bt[j] >= en - 0.5 * bar_len)
        if cands:
            best = min(cands, key=outside)
            if best > sb:
                talk, sb = best - sb, best
            song_end = float(bars[max(best, sb)]) + official
    res["Start"] = t(sb)
    v90 = np.percentile(voc, 90)
    vthr = 0.3 * v90
    vin = next((i for i in range(sb, nb - 1) if voc[i] > vthr and voc[i + 1] > vthr), None)
    res["Vocals"] = t(vin) if vin is not None and v90 > 0.01 else None
    # where the phrases really start: songs often open with a pickup bar or two (a whisper, a fill, a
    # build) before the first hit. Count 8-bar phrases from the hit, or every cue lands a bar early.
    anchor = phrase_anchor(every, sb, nb)
    phrases = list(range(anchor, nb - 7, 8))           # 8-bar phrases from the song's first downbeat hit
    levels = np.array([blk(beat, i) for i in phrases]) if phrases else np.array([0.0])
    full = float(np.percentile(levels, 75))
    last_voc = max([i for i in range(nb) if voc[i] > vthr], default=None)
    out = None
    if last_voc is not None:
        out = anchor + ((last_voc + 1 - anchor + 7) // 8) * 8
    if out is None or out > nb - 8:
        loud_tail = [i for i in phrases if blk(beat, i) >= 0.6 * full]
        out = (loud_tail[-1] + 8) if loud_tail else nb - 8
    out = int(min(max(out, anchor + 16), max(anchor + 16, nb - 4)))
    out = anchor + ((out - anchor) // 8) * 8 if out - anchor >= 16 else out
    lift = lambda i: blk(beat, i) / (blk(beat, i - 8) + 1e-9) if i - 8 >= sb else blk(beat, i) / (blk(beat, sb, max(1, i - sb)) + 1e-9)
    drop = next((i for i in phrases if anchor + 8 <= i < out - 8 and blk(beat, i) >= 0.85 * full and lift(i) > 1.1), None)
    if drop is None or drop > anchor + 0.6 * (out - anchor):
        cands = [i for i in phrases if anchor + 8 <= i <= anchor + 0.6 * (out - anchor)]
        drop = max(cands, key=lift, default=drop)
    res["Drop"] = t(drop) if drop is not None else None
    brk = drop2 = None
    if drop is not None:
        after = [i for i in phrases if drop + 8 <= i <= out - 8]
        dips = [i for i in after if blk(drm, i) < 0.5 * blk(drm, drop) or blk(beat, i) < 0.65 * blk(beat, drop)]
        brk = dips[0] if dips else (min(after, key=lambda i: blk(beat, i)) if after else None)
        if brk is not None:
            drop2 = next((i for i in phrases if brk + 8 <= i < out and blk(beat, i) >= 0.85 * full and lift(i) > 1.1), None)
    res["Breakdown"] = t(brk) if brk is not None else None
    res["Drop 2"] = t(drop2) if drop2 is not None else None
    # outro: where the song starts to wind down for good, on its 8-bar system from the first note: the first
    # phrase line after which every phrase stays clearly under the song's full level (the fade-out begins)
    ph8 = list(range(anchor, nb - 3, 8))
    lv8 = [blk(every, i) for i in ph8]
    full8 = float(np.percentile(lv8, 75)) if lv8 else 0.0
    fade = None
    for j in range(len(ph8) - 1, 0, -1):
        if lv8[j] >= 0.8 * full8:
            fade = ph8[j + 1] if j + 1 < len(ph8) else None
            break
    if fade is not None:
        out = fade
    res["Outro"] = t(out) if out < nb else None
    loud = drop if drop is not None else (max(phrases, key=lambda i: blk(beat, i)) if phrases else anchor)
    res["_intro_beat"] = blk(drm, sb) / (blk(drm, loud) + 1e-9)
    # how much of the first 8 bars has a voice sitting up front (within ~6 dB of the music). Ad-libs, shouts
    # and background chants are short or tucked under the beat, so another song can still be mixed over
    # them; a real verse or hook sits on top of the beat most of the time.
    sm = lambda x: np.convolve(x.astype("float64"), np.ones(6) / 6, "same")      # ~1/8 note: words, not frames
    a0, a1 = int(bt[sb] * SR / HOPL), int(bt[min(sb + 8, nb)] * SR / HOPL)
    vs, bs = sm(lev["vocals"][a0:a1]), sm(lev["beat"][a0:a1])
    prom = (vs > 0.25 * bs) & (vs > (0.15 * v90) ** 2)
    pb = [float(prom[int(bt[i] * SR / HOPL) - a0:int(bt[i + 1] * SR / HOPL) - a0].mean() or 0.0)
          for i in range(sb, min(sb + 8, nb))]
    res["_vox_share"] = float(np.mean(pb)) if pb else 0.0
    res["_vox_run"] = max([min(pb[i], pb[i + 1]) for i in range(len(pb) - 1)], default=0.0)
    # a cappella start / end: at least two whole bars of voice with next to no music (DJ-pool "Acap In" /
    # "Acap Out" versions). Talking before a music-video song was already cut off by the song window.
    acap = lambda i: 0 <= i < nb and voc[i] > 0.3 * v90 and beat[i] < 0.08 * b90
    e90 = float(np.percentile(every, 90))
    last = min(nb - 1, int(np.searchsorted(bars, song_end)) - 1) if song_end else nb - 1
    while last > sb and every[last] < 0.1 * e90:
        last -= 1
    res["_acap_in"] = float(acap(sb) and acap(sb + 1))
    res["_acap_out"] = float(last - 1 > sb + 8 and acap(last) and acap(last - 1))
    info = dict(sb=sb, anchor=anchor, loud=loud, drm=drm, every=every, talk=talk, end=song_end, voc=voc, beat=beat, full=full, v90=v90, nb=nb, phrases=phrases)
    return res, info


def _xjoin(parts, ms=4):
    """Join audio pieces cut on bar lines with a tiny crossfade so the cuts can't click."""
    # the outgoing bar fades over its last few ms; the incoming bar only gets a 0.3 ms ramp, so the hit on
    # the 1 keeps its full attack (a 4 ms fade-in there softened every downbeat and sounded cut off)
    k = int(ms / 1000 * SR)
    k_in = max(1, int(0.0003 * SR))
    out = parts[0].copy()
    for p in parts[1:]:
        p = p.copy()
        if len(out) > k and len(p) > k:
            out[-k:] *= np.linspace(1, 0, k, dtype="float32")[:, None]
            p[:k_in] *= np.linspace(0, 1, k_in, dtype="float32")[:, None]
        out = np.concatenate([out, p])
    return out


def bar_chroma(x, bars, sr=44100):
    m = (x.mean(axis=1) if x.ndim == 2 else x)[::2]; fs = sr / 2
    n = 4096; win = np.hanning(n)
    f = np.fft.rfftfreq(n, 1 / fs); ok = (f > 100) & (f < 4000)
    pc = (np.round(12 * np.log2(f[ok] / 440.0)) % 12).astype(int)
    out = []
    for i in range(len(bars) - 1):
        a, b = int(bars[i] * fs), int(bars[i + 1] * fs)
        c = np.zeros(12)
        for s0 in range(a, max(a + 1, b - n), n // 2):
            fr = m[s0:s0 + n]
            if len(fr) < n: break
            mag = np.abs(np.fft.rfft(fr * win))[ok]
            np.add.at(c, pc, mag)
        out.append(c / (np.linalg.norm(c) + 1e-9))
    return np.array(out)


def _lowpass(x, cutoff):
    """Smooth low-pass (FFT, overlap-add) for the filter-opening build at the start of an intro."""
    n, hop = 4096, 2048
    win = np.hanning(n).astype("float32")
    f = np.fft.rfftfreq(n, 1 / SR)
    resp = (1 / np.sqrt(1 + (f / cutoff) ** 4)).astype("float32")
    out = np.zeros_like(x)
    norm = np.zeros(len(x), "float32")
    for s0 in range(0, max(1, len(x) - n + hop), hop):
        fr = x[s0:s0 + n]
        m = len(fr)
        if m < 16:
            break
        w = win[:m]
        spec = np.fft.rfft(fr * w[:, None], n=n, axis=0) * resp[:, None]
        out[s0:s0 + m] += np.fft.irfft(spec, n=n, axis=0)[:m] * w[:, None]
        norm[s0:s0 + m] += w ** 2
    return (out / np.maximum(norm, 1e-3)[:, None]).astype("float32")


def _highpass(x, cutoff):
    return (x - _lowpass(x, cutoff)).astype("float32")


def _stft(m, n=2048, hop=512):
    w = np.hanning(n).astype("float32")
    m = np.pad(m, (n // 2, n // 2 + hop))
    idx = np.arange(0, len(m) - n, hop)
    return np.fft.rfft(np.stack([m[i:i + n] * w for i in idx]), axis=1).T, len(m)


def _istft(S, total, length, n=2048, hop=512):
    w = np.hanning(n).astype("float32")
    fr = np.fft.irfft(S.T, n=n, axis=1)
    out = np.zeros(total, "float32"); norm = np.zeros(total, "float32")
    for t, i in enumerate(range(0, total - n, hop)):
        if t >= len(fr):
            break
        out[i:i + n] += fr[t] * w; norm[i:i + n] += w * w
    out /= np.maximum(norm, 1e-6)
    return out[n // 2:n // 2 + length]


def _percussive(x, k=17):
    """Only the hits of a drum stem: anything that rings on a pitch (bass bleeding in, tuned drums, a guitar
    note left behind) is filtered out, so a carried beat can't clash with the song's chords. Harmonic /
    percussive split by median filtering: pitched sound is steady along time, hits are spread across
    frequency."""
    from numpy.lib.stride_tricks import sliding_window_view as sw
    mono = x.mean(axis=1)
    S, total = _stft(mono)
    mag = np.abs(S)
    H = np.median(sw(np.pad(mag, ((0, 0), (k // 2, k // 2)), mode="edge"), k, axis=1), axis=-1)
    P = np.median(sw(np.pad(mag, ((k // 2, k // 2), (0, 0)), mode="edge"), k, axis=0), axis=-1)
    mask = (P ** 2 / (H ** 2 + P ** 2 + 1e-12)).astype("float32")
    out = np.zeros_like(x)
    for c in range(x.shape[1]):
        Sc, _ = _stft(x[:, c])
        out[:, c] = _istft(Sc * mask, total, len(x))
    return out


def _hit_profile(x, steps=16):
    """Where in a bar the hits land: onset strength on each 16th note, loudest = 1."""
    m = x.mean(axis=1) if x.ndim == 2 else x
    n = len(m) // 256
    if n < steps * 2:
        return np.zeros(steps)
    e = np.log1p(1e3 * (m[:n * 256].reshape(n, 256) ** 2).mean(axis=1))
    d = np.maximum(np.diff(e, prepend=e[0]), 0)
    edges = np.linspace(0, n, steps + 1).astype(int)
    p = np.array([d[edges[i]:max(edges[i] + 1, edges[i + 1])].max() for i in range(steps)])
    return p / (p.max() + 1e-9)


def _pitch_mix(x):
    """How the sound's energy spreads over the 12 notes (60 Hz - 2 kHz), summing to 1."""
    m = x.mean(axis=1) if x.ndim == 2 else x
    F = np.abs(np.fft.rfft(m * np.hanning(len(m)))) ** 2
    f = np.fft.rfftfreq(len(m), 1 / SR)
    ok = (f > 60) & (f < 2000)
    c = np.zeros(12)
    np.add.at(c, (np.round(69 + 12 * np.log2(f[ok] / 440.0)).astype(int)) % 12, F[ok])
    return c / (c.sum() + 1e-12)


def _corr(a, b):
    if np.std(a) < 1e-6 or np.std(b) < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


CARRY_DEBUG = None      # tests only: collects every candidate loop
CARRY_RHYTHM = 0.15     # carried beat's hits vs the opening's own hits (1 = identical)
CARRY_PITCH = 0.45      # how much the carried layer's notes agree with the opening's (higher = no clash)


def _riser(n, seed=7):
    """Filtered-noise sweep that climbs in pitch and level over n samples (the classic build riser)."""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal((n, 2)).astype("float32") * 0.25
    out = np.zeros_like(noise)
    blocks = 16
    edges = np.linspace(0, n, blocks + 1).astype(int)
    for b in range(blocks):
        a, e = edges[b], edges[b + 1]
        fc = 400 * (12000 / 400) ** (b / (blocks - 1))
        out[a:e] = _highpass(noise[a:e], fc)
    env = np.linspace(0, 1, n, dtype="float32") ** 2
    return out * env[:, None]


def _roll(bar, drum_bar):
    """Snare-roll style stutter over a bar: a 16th-note slice of the drums from beat 2 (where the snare
    usually sits) repeated as 8ths, then 16ths, then 32nds, getting louder; the last 8th is left silent."""
    n = len(bar)
    sl = n // 16
    src = drum_bar[n // 4: n // 4 + sl].copy()
    if len(src) < 32:
        return bar
    f = min(64, len(src) // 4)
    src[:f] *= np.linspace(0, 1, f, dtype="float32")[:, None]
    src[-f:] *= np.linspace(1, 0, f, dtype="float32")[:, None]
    out = np.zeros_like(bar)
    plan = [(0, n // 2, n // 8), (n // 2, 3 * n // 4, n // 16), (3 * n // 4, 7 * n // 8, n // 32)]
    for a, e, step in plan:
        for t in range(a, e, max(1, step)):
            g = 0.5 + 0.5 * t / n
            m = min(len(src), n - t, step if step < len(src) else len(src))
            out[t:t + m] += src[:m] * g
    return out


def build_layers(drums, inst, dbars, ibars, mode, tension, bass_from=0, dance=False):
    """Per-bar layering for an intro (dbars / ibars: the drum stem and instrumental for each source bar).
    Built the way DJ-pool intros are (measured on 18 pool intros against their originals): the beat with
    its full low end (kick and bass) from the first bar, the music held back in the mids and opening up
    through a filter in the second half, so the energy still climbs into the song. A song that opens soft
    gets drums only (no bass to clash with its opening); when the intro's music would repeat the song's
    own opening (a one-loop song) the music waits for the song."""
    n = len(dbars)
    out = []
    for i in range(n):
        d, x = dbars[i].astype("float32"), ibars[i].astype("float32")
        nd = x - d                                             # the music without the drums
        bass = _lowpass(nd, 220.0)
        if mode == "soft":
            bar = d
        else:
            # the song's own backbeat and melody are there from the first bar (a bare drum loop sounds dull,
            # most of all in front of a high-energy song). DJ pools keep the whole instrumental of the hook in
            # nearly flat (their music only ~0.4 dB fuller in the second half), so: barely filtered, a touch
            # down at first, opening up to the full instrumental by the last bar: a slight build
            frac = i / max(1, n - 1)
            if frac < 1:
                music = nd - bass                    # full range, no filter sweep: only the level comes up a touch
                bar = d + bass + music * (0.8 + 0.2 * frac)
            else:
                bar = d + nd
            if dance and frac < 1:
                # house / dance intros are the drums and the bassline: the separated melody (where separation
                # leaves its watery artifacts) only comes in, filtered, over the last two bars
                bar = d + bass + (music * max(0.0, (frac - (n - 3) / max(1, n - 1)) * (n - 1) / 2) if n > 2 else 0)
            if i < bass_from:          # 16-bar intros: the bass line waits for Halfway (the kick keeps its low end)
                bar = bar - bass
        out.append(bar.astype("float32"))
    if tension:
        apply_tension(out, dbars[-1].astype("float32"))
    return out


def apply_tension(out, drum_bar):
    """Last bar of a build: kick out (high-pass), snare-roll stutter, a riser over the last two bars,
    and the final 8th left silent so the song's first hit lands with full impact."""
    n = len(out)
    if n == 0:
        return out
    body = _highpass(out[-1], 120.0)
    bar = body + _roll(body, drum_bar) * 0.9
    bar[-len(bar) // 16:] *= 0.0                      # a 16th of silence before the song hits
    out[-1] = bar.astype("float32")
    if n >= 2:
        r = _riser(len(out[-2]) + len(out[-1]))
        lvl = float(np.sqrt(np.mean(np.concatenate(out[-2:]) ** 2))) + 1e-9
        r *= 0.35 * lvl / (float(np.sqrt(np.mean(r ** 2))) + 1e-9)
        out[-2] = out[-2] + r[:len(out[-2])]
        tail = r[len(out[-2]):].copy()
        k = len(tail) // 8
        if k > 0:
            tail[-k:] *= np.linspace(1, 0, k, dtype="float32")[:, None]
        out[-1] = out[-1] + tail
    return out


def _limit(x, ceiling=0.95, block=128):
    """Transparent peak limiter: only the loudest hits are turned down (fast attack, ~60 ms release), so an
    intro can be brought up to the song's level without clipping or turning the whole thing down."""
    n = len(x)
    nb_ = (n + block - 1) // block
    pad = np.zeros((nb_ * block - n, x.shape[1]), "float32")
    pk = np.abs(np.concatenate([x, pad])).reshape(nb_, block, -1).max(axis=(1, 2))
    g = np.minimum(1.0, ceiling / np.maximum(pk, 1e-9))
    g = np.minimum(g, np.concatenate([g[1:], [1.0]]))          # start turning down one block early
    rel = np.exp(-block / (0.06 * SR))
    for i in range(1, nb_):                                     # recover slowly after a hit
        g[i] = min(g[i], g[i - 1] + (1 - g[i - 1]) * (1 - rel))
    gs = np.repeat(g, block)[:n].astype("float32")
    y = x * gs[:, None]
    return np.clip(y, -0.99, 0.99).astype("float32")


TRIM_HEADROOM = 0.5     # dB kept under full scale so Serato's meters never reach the red (MP3 peaks overshoot a bit)


def trim_db(audio):
    """Serato track gain that makes the song as loud as it can be without clipping: its loudest peak
    lifted to just under full scale (0 when it already peaks there, never turned down)."""
    pk = float(np.max(np.abs(audio))) + 1e-9
    return float(np.clip(-TRIM_HEADROOM - 20 * np.log10(pk), 0.0, 12.0))


def set_trim(path, audio, bpm):
    """Write the gain into Serato's Autotags (bpm, auto gain, gain dB), keeping Serato's own auto-gain value."""
    from mutagen.id3 import ID3, GEOB
    t = ID3(str(path))
    old = next((f.data for f in t.getall("GEOB") if f.desc == "Serato Autotags"), None)
    parts = old[2:].split(b"\x00") if old else []
    auto = parts[1] if len(parts) >= 2 and parts[1] else b"0.000"
    cur = parts[2].decode(errors="ignore") if len(parts) >= 3 and parts[2] else "0.000"
    ours = next((str(f) for f in t.getall("TXXX:DJ Grab trim")), None)
    try:
        cur_v = float(cur)
    except ValueError:
        cur_v = 0.0
    if (ours is None and abs(cur_v) > 0.001) or (ours is not None and abs(cur_v - float(ours)) > 0.001):
        return None                       # you set this gain yourself (or changed ours): it stays as it is
    g = trim_db(audio)
    from mutagen.id3 import TXXX
    t.setall("TXXX:DJ Grab trim", [TXXX(encoding=3, desc="DJ Grab trim", text=f"{g:.3f}")])
    data = b"\x01\x01" + f"{float(bpm):.2f}".encode() + b"\x00" + auto + b"\x00" + f"{g:.3f}".encode() + b"\x00"
    t.delall("GEOB:Serato Autotags")
    t.add(GEOB(encoding=0, mime="application/octet-stream", desc="Serato Autotags", data=data))
    t.save(str(path), v2_version=3 if t.version[1] == 3 else 4)
    return g


class OpensOnHook(Exception):
    pass


def _low_bars(orig, bt, cut=150.0):
    """Per-bar level of the low end (kick and bass, under ~150 Hz) of the full mix. Claps, snaps and voices
    barely register here, so it shows whether a song's beat is really playing."""
    m = orig.mean(axis=1) if orig.ndim == 2 else orig
    d = 32
    n = len(m) // d
    x = m[:n * d].reshape(n, d).mean(axis=1)              # ~1.4 kHz, plenty for under 150 Hz
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), d / SR)
    X[f > cut] = 0
    lo = np.fft.irfft(X, len(x))
    out = np.zeros(len(bt) - 1)
    for i in range(len(bt) - 1):
        a, b = int(bt[i] * SR / d), int(bt[i + 1] * SR / d)
        out[i] = float(np.sqrt(np.mean(lo[a:b] ** 2))) if b > a else 0.0
    return out


def _tilt(x, db=3.5):
    """Everything above the bass up by `db` against the bass (the level is set afterwards anyway). Measured on
    the pool test: DJ Grab's intros sat 3-4 dB under the pools' in the low mids, mids and highs (the separated
    instrumental comes out a little dull) with the bass right on, so this evens it out."""
    lo = _lowpass(x, 150.0)
    return (lo + (x - lo) * (10 ** (db / 20))).astype("float32")


def _drum_purity(x, k=17):
    """How cleanly a drum-stem bar came out of the separation: the share of its sound that is hits, not
    smeared, ringing leftovers of the vocals or music (what made Butter's snare sound "crappy"). 0..1"""
    from numpy.lib.stride_tricks import sliding_window_view as sw
    mono = x.mean(axis=1) if x.ndim == 2 else x
    if len(mono) < 4096:
        return 1.0
    S, _ = _stft(mono)
    mag = np.abs(S)
    H = np.median(sw(np.pad(mag, ((0, 0), (k // 2, k // 2)), mode="edge"), k, axis=1), axis=-1)
    P = np.median(sw(np.pad(mag, ((k // 2, k // 2), (0, 0)), mode="edge"), k, axis=0), axis=-1)
    mask = P ** 2 / (H ** 2 + P ** 2 + 1e-12)
    e = mag ** 2
    return float((mask * e).sum() / (e.sum() + 1e-12))


def _band_db(x):
    m = x.mean(axis=1) if x.ndim == 2 else x
    F = np.abs(np.fft.rfft(m)) ** 2
    f = np.fft.rfftfreq(len(m), 1 / SR)
    b = lambda lo, hi: 10 * np.log10(F[(f >= lo) & (f < hi)].sum() / max(1, len(m)) + 1e-12)
    return np.array([b(20, 150), b(150, 500), b(500, 2500), b(2500, 10000)])


# what 150 DJ-pool intros in Vinny's library sound like, against their own song (dB vs the song's median bar):
# full bass, the music without vocals ~6 dB under in the mids, highs ~3 dB under
POOL_PROFILE = np.array([0.0, -2.0, -6.0, -3.0])


def _vocals_of(stems):
    try:
        return stems["vocals"]
    except Exception:
        return None


def _vocal_leak_db(inst_bar, voc_bar):
    """How much of the singing is left in an instrumental bar (dB against the bar): the part of the bar that
    moves with the separated vocals. Under about -30 dB nobody hears it; a ghost of the singer is -15 or so."""
    n = min(len(inst_bar), len(voc_bar))
    if n < 2048:
        return -99.0
    x = inst_bar[:n].mean(axis=1) if inst_bar.ndim == 2 else inst_bar[:n]
    v = voc_bar[:n].mean(axis=1) if voc_bar.ndim == 2 else voc_bar[:n]
    vv = float(v @ v)
    if vv < 1e-6 * n:                       # nobody singing in this bar
        return -99.0
    g = float(x @ v) / vv
    resid = g * g * vv
    return 10 * np.log10(resid / (float(x @ x) + 1e-12) + 1e-12)


# "Try another intro": versions already heard for a song ("<source bar>:<loop>"), so a redo never comes
# back with the same one. When every version from one stretch of the song was tried, the next stretch is used.
TRIED_FILE = djgrab.APP_SUPPORT / "intro_tries.json"
REDO_TRIED = set()
REDO_PICK = None
# The shared app (everyone but the owner): cue points and intro versions only. No renaming or tidying of
# their files and no DJ-software crates, so nothing of theirs is changed beyond what they asked for.
PLAIN = os.environ.get("DJGRAB_PLAIN") == "1"
if PLAIN:
    djgrab.queue_for_crate = lambda *a, **k: None
    djgrab.flush_crates = lambda *a, **k: None
REDO_SKIP_BEST = False      # a redo with no record of the first intro: assume it was the best-scoring one


_TASTE = None


def _taste():
    """What your 👍 / 👎 taught: for each genre, how much you liked each kind of intro (loop A, loop B,
    straight). +1 per thumbs up, -1 per thumbs down, read from your ratings and the version each intro used."""
    global _TASTE
    if _TASTE is None:
        _TASTE = {}
        try:
            ratings = json.loads((djgrab.APP_SUPPORT / "intro_ratings.json").read_text())
            tries = json.loads(TRIED_FILE.read_text())
        except Exception:
            ratings, tries = [], {}
        for r in ratings:
            p = unicodedata.normalize("NFC", r.get("path", "").replace(" (Intro).mp3", ".mp3"))   # rated = the intro
            picks = tries.get(p)
            if not picks:
                continue
            kind = picks[-1].split(":", 1)[1]
            parts = Path(p).parts
            top = parts[-3] if len(parts) >= 3 else ""
            for g in (top, "*"):
                _TASTE[(g, kind)] = _TASTE.get((g, kind), 0) + (1 if r.get("rating", 0) > 0 else -1)
    return _TASTE


class _Filled(np.ndarray):
    pass


def _fill_gaps(intro, song, parts, lens):
    """No dead air right after the handover: some songs stop for a moment just after they come in (Manchild
    goes quiet for ~0.3 s after "boy"). Over the song's first two bars, any near-silent stretch of 0.12 s
    or more gets the intro's beat played under it, in time (same spot in the bar), without its bass,
    faded in and out, so the groove carries straight through and stops right where the song hits again."""
    if not len(parts) or len(song) == 0:
        return song
    bar = int(lens[-1]) if len(lens) else 0
    src = parts[-2] if len(parts) >= 2 else parts[-1]          # the last full bar (the very last can be a tension bar)
    if bar <= 0 or len(src) < bar // 2:
        return song
    src = _highpass(src.astype("float32"), 150.0)              # no bass: it won't fight the song's own
    w = int(0.03 * SR)
    span = min(len(song), 2 * bar)
    mono = song[:span].mean(axis=1) if song.ndim == 2 else song[:span]
    env = np.array([np.sqrt(np.mean(mono[i:i + w] ** 2)) for i in range(0, span - w, w)])
    if not len(env):
        return song
    ref = float(np.sqrt(np.mean(intro[-bar:] ** 2))) + 1e-9
    quiet = env < ref * 10 ** (-32 / 20)                      # 32 dB under the intro's last bar
    out, filled, i = song.astype("float32").copy(), False, 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            a, b = i * w, j * w
            # a short real gap (a breath, a stop of under one beat), not the song's very first moment. Longer
            # stops are part of the song (a break before the drop) and are left alone: a beat played into
            # them sounds like two drummers out of time
            if int(0.12 * SR) <= b - a <= bar // 4 and a > 0:
                pos = np.arange(a, b) % len(src)
                fill = src[pos] * 10 ** (-4 / 20)
                f = min(int(0.02 * SR), (b - a) // 4)
                ramp = np.ones(b - a, dtype="float32")
                ramp[:f] = np.linspace(0, 1, f); ramp[-f:] = np.linspace(1, 0, f)
                out[a:b] += fill * (ramp[:, None] if out.ndim == 2 else ramp)
                filled = True
            i = j
        else:
            i += 1
    out = np.clip(out, -1, 1)
    if filled:
        out = out.view(_Filled); out.filled = True
    return out


def _intro_cost(parts, loop, drums, seg, C, orig, bt, nb, sb, inst=None, vocals=None):
    """Score one intro version (lower is better): gaps, a loop that jumps at its seam, separation smear in
    the drums, sounding unlike a pool intro, and a big change in sound where the song takes over."""
    cost, why = 0.0, []
    intro = np.concatenate(parts)
    beat = max(1, int(len(intro) / max(1, len(parts)) / 4))
    lv = np.array([float(np.sqrt(np.mean(intro[k:k + beat] ** 2))) for k in range(0, len(intro) - beat + 1, beat)])
    if len(lv) and lv.min() < 0.1 * np.median(lv):
        cost += 10; why.append("gap")
    if C is not None and len(set(loop)) < len(loop):
        a, b = loop[len(set(loop)) - 1], loop[0]          # the loop's last bar runs into its first again
        nat = min(a + 1, len(C) - 1)
        seam = float(C[nat] @ C[b] / (np.linalg.norm(C[nat]) * np.linalg.norm(C[b]) + 1e-9))
        cost += (1 - seam) * 4; why.append(f"seam {seam:.2f}")
    if inst is not None:
        def voc_bar(i):
            v = seg(vocals, i, i + 1) if vocals is not None else None
            if v is None or len(v) < 2048 or float(np.abs(v).max()) < 1e-4:
                v = seg(orig, i, i + 1)[:len(seg(inst, i, i + 1))] - seg(inst, i, i + 1)   # what the separation took out
            return v
        leak = max(_vocal_leak_db(seg(inst, i, i + 1), voc_bar(i)) for i in sorted(set(loop)))
        if leak > -99:
            cost += float(np.clip((leak + 30) / 5, 0, 4)); why.append(f"vocals left {leak:.0f} dB")
    pur = float(np.mean([_drum_purity(seg(drums, i, i + 1)) for i in sorted(set(loop))]))
    cost += (1 - pur) * 4; why.append(f"drums {pur:.2f}")
    ref = np.median([_band_db(seg(orig, i, i + 1)) for i in range(0, nb, max(1, nb // 24))], axis=0)
    prof = np.median([_band_db(p) for p in parts], axis=0) - ref
    dev = float(np.mean(np.abs(prof - POOL_PROFILE)))
    cost += dev / 3; why.append(f"sound {dev:.1f} dB off")
    if sb < nb:
        jump = float(np.mean(np.abs(_band_db(parts[-1]) - _band_db(seg(orig, sb, sb + 1)))))
        cost += jump / 6; why.append(f"handover {jump:.1f} dB")
    return cost, ", ".join(why)


LAYER_GENRES = {"African", "Caribbean", "Latin"}     # where DJ pools layer the beat under a soft opening


def _snap_bars(orig, bars):
    """Each bar line moved onto the downbeat hit it belongs to (within 50 ms), so a looped bar starts
    exactly on the hit instead of a hair after it (a clipped first hit sounds off the beat) or before it
    (a tiny gap). Live-played songs drift around the grid, so this matters most there. Bars with no clear
    hit near them stay where they are."""
    try:
        m = orig.mean(axis=1) if orig.ndim == 2 else orig
        hop = int(0.004 * SR)
        n = len(m) // hop
        env = np.sqrt((m[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
        on = np.maximum(0.0, np.diff(env, prepend=env[0]))
        ref = float(np.median(on[on > 0])) + 1e-9 if np.any(on > 0) else 1e-9
        out = np.asarray(bars, float).copy()
        w = int(0.05 / 0.004)
        for i, t in enumerate(out):
            c = int(t / 0.004)
            lo, hi = max(1, c - w), min(n - 1, c + w)
            if hi <= lo:
                continue
            j = lo + int(np.argmax(on[lo:hi]))
            if on[j] > 6 * ref:
                # down to the sample: the first moment the hit rises out of what was before it, then 2 ms earlier
                a_, b_ = max(0, (j - 4) * hop), min(len(m), (j + 2) * hop)
                x = np.abs(m[a_:b_])
                if len(x):
                    pk = float(x.max())
                    base = float(np.percentile(np.abs(m[max(0, a_ - 4 * hop):a_ + 1]), 90)) if a_ > 0 else 0.0
                    k = np.nonzero(x > max(0.2 * pk, 2.0 * base))[0]
                    # loud already at the start of the window (dense music): no clear attack to find, so
                    # the frame estimate is kept rather than pulling the bar line early
                    onset = (a_ + int(k[0])) if len(k) and k[0] > hop // 2 else j * hop
                    out[i] = max(0.0, onset / SR - 0.002)
                else:
                    out[i] = max(0.0, j * 0.004 - 0.004)
        return out
    except Exception:
        return np.asarray(bars, float)


CLAP_NEXT = False
HOOK_MODE = {"1": "fits", "0": "off"}.get(os.environ.get("DJGRAB_HOOK", "fits"), os.environ.get("DJGRAB_HOOK", "fits"))
HOOK_TEASE = HOOK_MODE in ("fits", "always")     # Settings ▸ Intros ▸ Hook tease: Off / When it fits / Always
HOOK_GENRES = {"Hip Hop", "R&B", "Pop", "Latin", "African", "Caribbean"}
MAKE_OUTRO = os.environ.get("DJGRAB_OUTRO", "0") == "1"   # Settings ▸ Intros ▸ Outro versions


def _clap_hit(orig, stems, bt, info):
    """The song's own clap / snare, as one hit: from the loud part of the song, on beats 2 and 4, drums
    stem high-passed (no kick), the cleanest of the hits found. Using the song's own sound keeps it fitting."""
    try:
        d = stems["drums"]
    except Exception:
        d = orig
    loud = int(info.get("loud", 0))
    hop = int(0.004 * SR)
    best, score = None, -1.0
    for b in range(loud, min(loud + 8, len(bt) - 2)):
        L = bt[b + 1] - bt[b]
        for k in (1, 3):                              # beats 2 and 4
            t = bt[b] + k * L / 4
            a = int((t - 0.01) * SR)
            if a < 0 or a + int(0.2 * SR) > len(d):
                continue
            w = _highpass(d[a:a + int(0.2 * SR)].astype("float32"), 400.0)
            env = np.abs(w).mean(axis=1) if w.ndim == 2 else np.abs(w)
            peak = float(env[: int(0.03 * SR)].max())
            tail = float(env[int(0.12 * SR):].mean()) + 1e-9
            sc = peak / tail                          # a sharp hit that dies away = a clean clap
            if sc > score:
                best, score = w, sc
    if best is None:
        return None
    fade = np.linspace(1, 0, len(best), dtype="float32") ** 2
    best = best * (fade[:, None] if best.ndim == 2 else fade)
    return best / (float(np.abs(best).max()) + 1e-9)


def build_clap_intro(orig, stems, bars, info, n_bars):
    """A clap build (good for singalongs and songs with a light or no beat): the song's own clap on 2 and
    4, then on every beat, then eighths, then a sixteenth-note roll that rises into the song, the way a crowd
    claps it in. Returns the same things as build_intro_bars."""
    bt = np.append(_snap_bars(orig, bars), len(orig) / SR)
    sb = int(info["sb"])
    hit = _clap_hit(orig, stems, bt, info)
    if hit is None:
        raise RuntimeError("no clap or snare found in the song")
    L = float(np.median(np.diff(bt[sb:sb + 9]))) if len(bt) > sb + 2 else float(np.median(np.diff(bt)))
    bar = int(round(L * SR))
    intro = np.zeros((bar * n_bars, orig.shape[1] if orig.ndim == 2 else 1), dtype="float32")
    n = n_bars
    for i in range(n):
        if i < n // 2:
            pos, g = [1, 3], 0.55                                   # 2 and 4
        elif i < n - 2 or n <= 2:
            pos, g = [0, 1, 2, 3], 0.65                             # every beat
        elif i == n - 2:
            pos, g = [x / 2 for x in range(8)], 0.75                # eighths
        else:
            pos, g = [x / 4 for x in range(16)], 0.85               # sixteenths, rising
        for j, p in enumerate(pos):
            a = i * bar + int(p * bar / 4)
            seg = hit[: max(1, min(len(hit), int(bar / len(pos)) if len(pos) > 4 else len(hit)))]
            gg = g * (0.6 + 0.4 * j / max(1, len(pos) - 1)) if i == n - 1 else g
            e = min(len(intro), a + len(seg))
            intro[a:e] += (seg[: e - a] if seg.ndim == 2 else seg[: e - a, None]) * gg
    # as loud as the song where it comes in
    song = orig[int(round(bt[sb] * SR)):].astype("float32")
    r_song = float(np.sqrt(np.mean(song[: bar * 2] ** 2))) + 1e-9
    r_in = float(np.sqrt(np.mean(intro[-bar * 2:] ** 2))) + 1e-9
    intro *= min(4.0, 0.8 * r_song / r_in)
    intro = np.clip(intro, -1, 1)
    gen_bars = np.arange(n) * L
    audio = _xjoin([intro, song])
    return audio, gen_bars, "clap build (the song's own clap, 2 & 4 into a roll)", "Halfway", len(intro) / SR


def _hook_votes(top):
    """Your 👍 / 👎 on intros that had a hook tease, for this genre: ups minus downs."""
    try:
        ratings = json.loads((djgrab.APP_SUPPORT / "intro_ratings.json").read_text())
    except Exception:
        return 0
    n = 0
    for r in ratings:
        if "hook teased" not in str(r.get("details", "")):
            continue
        parts = Path(r.get("path", "")).parts
        if len(parts) >= 3 and parts[-3] == top:
            n += 1 if r.get("rating", 0) > 0 else -1
    return n


def _frames_rms(x, ms=50):
    k = max(1, int(SR * ms / 1000))
    m = len(x) // k
    if m == 0:
        return np.zeros(0)
    y = x[:m * k].reshape(m, k, -1)
    return np.sqrt((y ** 2).mean(axis=(1, 2)))


def hook_tease(intro, lens, orig, stems, bt, info, style, sb):
    """The first 2 bars of the chorus vocal, 6 dB under the beat, placed so 4 bars of beat alone are left
    before the song comes in (bars 3-4 of an 8-bar intro, 11-12 of a 16-bar one). "When it fits" only
    adds it when every rule passes; returns (intro, reason)."""
    gen = len(lens)
    at = gen - 6                                  # 2 bars of hook, then 4 bars of beat only
    if at < 1:
        return intro, "intro too short for a tease and a 4-bar gap"
    v = _vocals_of(stems)
    if v is None:
        return intro, "no vocal stem"
    nb = int(info["nb"])
    c = int(info.get("chorus_bar") if info.get("chorus_bar") is not None else info["loud"])
    if c + 2 >= len(bt):
        return intro, "no chorus found"
    a_, b_ = int(round(bt[c] * SR)), int(round(bt[c + 2] * SR))
    vb = v[a_:b_].astype("float32")
    if len(vb) < SR or float(np.sqrt(np.mean(vb ** 2))) < 1e-3:
        return intro, "no singing at the chorus"
    always = HOOK_MODE == "always"
    if not always:
        top = info.get("top", "")
        if top not in HOOK_GENRES:
            return intro, f"not used for {top or 'this genre'}"
        if _hook_votes(top) <= -3:
            return intro, f"turned off for {top} by your ratings"
        if c - sb <= 2:
            return intro, "song opens on its chorus"
        # compared on the VOCAL (the beat of a hip hop song is the same loop all the way through)
        C = bar_chroma(v[:len(orig)], bt)
        h = C[c:c + 2]
        sim = lambda k: float(np.mean([float(h[j] @ C[k + j]) for j in range(2)])) if k + 2 <= len(C) else 0.0
        voc = info["voc"]
        if float(np.mean(voc[sb:sb + 2])) >= 0.35 * info["v90"] and sim(sb) >= 0.9:
            return intro, "song opens with the hook's vocal"
        if not any(sim(k) >= 0.85 for k in range(0, nb - 1) if abs(k - c) >= 8 and float(np.mean(voc[k:k + 2])) >= 0.35 * info["v90"]):
            return intro, "chorus doesn't repeat (not a clear hook)"
        fr = _frames_rms(vb)
        act = float(np.mean(fr > 0.25 * fr.max())) if len(fr) else 0.0
        if not 0.2 <= act <= 0.85:
            return intro, f"vocal isn't a short hook line ({act:.0%} sung)"
        e = max(1, len(fr) // 16)
        # sung lines often hold a note into the bar line, so only a word cut off hard (the bar edge as loud
        # as the line itself) counts; the tease fades in and out over 60 ms anyway
        if float(fr[:e].mean()) > 0.9 * float(fr.mean()) and float(fr[-e:].mean()) > 0.9 * float(fr.mean()):
            return intro, "the line runs over the bar lines"
        d = stems.get("drums") if isinstance(stems, dict) else None
        if d is not None:
            fd = _frames_rms(d[a_:b_].astype("float32"))
            n_ = min(len(fd), len(fr))
            if n_ > 4:
                ov, od = np.diff(fr[:n_]), np.diff(fd[:n_])
                if float(np.corrcoef(ov, od)[0, 1]) >= 0.5:
                    return intro, "vocal has drum bleed"
        if not style.startswith("drums"):
            s0 = int(sum(lens[:at]))
            seg_ = intro[s0:s0 + len(vb)]
            p1, p2 = _pitch_mix(vb), _pitch_mix(seg_)
            cos = float(p1 @ p2 / (np.linalg.norm(p1) * np.linalg.norm(p2) + 1e-9))
            if cos < 0.8:
                return intro, f"hook's key doesn't match the intro's music ({cos:.2f})"
    s0 = int(sum(lens[:at]))
    n2 = int(sum(lens[at:at + 2]))
    vb = _highpass(vb[:n2], 180.0)
    tail = intro[s0:s0 + len(vb)]
    g = (float(np.sqrt(np.mean(tail ** 2))) + 1e-9) / (float(np.sqrt(np.mean(vb ** 2))) + 1e-9) * 10 ** (-6 / 20)
    f = min(int(0.06 * SR), len(vb) // 4)
    env = np.ones(len(vb), "float32")
    env[:f] = np.linspace(0, 1, f); env[-f:] = np.linspace(1, 0, f)
    out = intro.copy()
    out[s0:s0 + len(vb)] = _limit(out[s0:s0 + len(vb)] + vb * g * env[:, None])
    return out, f"bars {at + 1}-{at + 2}"


INTRO_NAME = re.compile(r"\bintro\b", re.I)


def intro_halfway(path, bars_t, sb0, times):
    """Seconds of the Halfway cue for an intro version: half way (on a 2-bar line) from its first bar to where
    the vocals come in. None for a normal song, or when the intro is under 4 bars."""
    if not (INTRO_NAME.search(Path(path).stem) or is_our_intro(path)):
        return None
    v = times.get("Vocals")
    if not isinstance(v, float):
        return None
    vb = int(np.argmin(np.abs(np.asarray(bars_t) - v)))
    n = vb - sb0
    if n < 4:
        return None
    h = sb0 + (n // 2) // 2 * 2
    return float(bars_t[h])


def _has_halfway(p):
    try:
        from mutagen.id3 import ID3
        g = next((f.data for f in ID3(str(p)).getall("GEOB") if f.desc == "Serato Markers2"), b"")
        return any(n == "CUE" and read_cue(x)["name"] == "Halfway" for n, x in parse_markers2(g))
    except Exception:
        return False


def intro_files_missing_halfway(root):
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root)
        dn[:] = sorted(d for d in dn if not d.startswith(("_", ".")) and not (rel == "." and d in SKIP_FOLDERS))
        for f in sorted(fn):
            if f.startswith(".") or not f.lower().endswith(".mp3") or ".part." in f or not INTRO_NAME.search(f):
                continue
            if re.search(r"acap|instrumental|\binst\b|transition", f, re.I):
                continue
            p = os.path.join(dp, f)
            if not _has_halfway(p):
                yield p


def outro_path(p):
    """"Artist - Title (Outro).mp3" next to the song."""
    p = Path(p)
    return p.with_name(f"{p.stem} (Outro){p.suffix}")


def build_outro(orig, stems, bars, info, n_out=16):
    """DJ outro: the song as it is until its vocals end (on a 4-bar line), then n_out bars of its own beat
    with no vocals to mix out of: the full instrumental for 8 bars, then drums and bass, then drums alone,
    ending on the bar. None when the song already has 16 bars of beat after its last vocal.
    Returns (audio, seconds where the outro beat starts, bar starts of the added bars)."""
    nb, voc, drm, v90 = int(info["nb"]), info["voc"], info["drm"], info["v90"]
    sb = int(info.get("anchor", info["sb"]))
    bt = np.append(_snap_bars(orig, bars), len(orig) / SR)
    sung = [i for i in range(nb) if voc[i] >= 0.35 * v90]
    if not sung:
        return None
    lv = sung[-1]
    dref = float(np.median(drm[drm > 0])) if np.any(drm > 0) else 0.0
    run = 0
    for j in range(lv + 1, nb):
        if drm[j] < 0.4 * dref:
            break
        run += 1
    if run >= 16:
        return None                                   # it already ends with a beat to mix out of
    cut = lv + 1
    while (cut - sb) % 4 and cut < nb:
        cut += 1
    cut = min(cut, nb)
    seg = lambda x, i, j: x[int(round(bt[i] * SR)):int(round(bt[j] * SR))]
    loud = int(info["loud"])
    beat_ok = lambda i: i + 8 <= nb and float(drm[i:i + 8].min()) >= 0.4 * dref
    src = min((i for i in range(sb, nb - 7, 4) if beat_ok(i)), key=lambda i: abs(i - loud), default=None)
    if src is None:
        return None
    hq = hq_stems(orig, bt[src], bt[src + 8])
    if hq is not None:
        inst, drums = hq["inst"], hq["drums"]
    else:
        inst = orig[:len(stems["vocals"])] - stems["vocals"][:len(orig)]
        drums = stems["drums"]
    parts = []
    for k in range(n_out):
        i = src + (k % 8)
        d = seg(drums, i, i + 1).astype("float32")
        x = seg(inst, i, i + 1).astype("float32")
        nd = x - d
        bass = _lowpass(nd, 220.0)
        bar = d + nd if k < 8 else d + bass if k < 12 else d
        parts.append(bar.astype("float32"))
    last = parts[-1]
    f = len(last) // 4
    last[-f:] *= np.linspace(1, 0, f, dtype="float32")[:, None] ** 2
    tail = np.clip(_xjoin(parts), -1, 1).astype("float32")
    # as loud as the song's last 4 bars before the cut
    ref = seg(orig, max(sb, cut - 4), cut)
    r_ref = float(np.sqrt(np.mean(ref ** 2))) + 1e-9
    r_t = float(np.sqrt(np.mean(tail[:len(tail) // 2] ** 2))) + 1e-9
    tail = _limit(tail * float(np.clip(r_ref / r_t, 0.25, 4.0)))
    head = orig[:int(round(bt[cut] * SR))].astype("float32")
    lens = [len(p) for p in parts]
    starts = float(bt[cut]) + np.concatenate([[0], np.cumsum(lens)[:-1]]) / SR
    return _xjoin([head, tail]), float(bt[cut]), starts


def build_intro_bars(orig, stems, bars, info, n_bars):
    """DJ-pool style intro: exactly n_bars real bars from the start of the file to the song's first hit,
    cut on the song's own bar lines so every phrase lands on beat one.
    If the song opens with a short pickup (a whisper, a fill) before its first hit, that pickup becomes the
    last bar(s) of the intro, with the intro's drums carried under it, so the intro rolls into the song
    instead of dropping out.
    Best case: the song already has a stretch with no vocals and a full beat: use that ORIGINAL audio
    untouched. Otherwise: the hook with the vocals taken out (studio stems), drums only first, then the
    full instrumental.
    Returns (audio, bar start times of the new bars, style, halfway cue name, seconds to the song's first bar)."""
    nb, sb, voc, beat, full, v90 = info["nb"], info["sb"], info["voc"], info["beat"], info["full"], info["v90"]
    anchor = info.get("anchor", sb)
    pick = anchor - sb if 0 < anchor - sb <= 4 and anchor - sb < n_bars else 0
    if pick and float(voc[sb:anchor].max()) >= 0.35 * v90:
        pick = 0
    ev_ = info.get("every")
    if pick and ev_ is not None:
        # a whole bar of the song played as the intro's last bar only if it is a real pickup (a fill or a hit
        # well under the song): otherwise the song is heard starting a bar before the intro's 8 are up
        lv_ = lambda i, n: 20 * np.log10(float(np.mean(ev_[i:i + n])) + 1e-9)
        if lv_(sb, anchor - sb) > lv_(anchor, 4) - 8.0:      # same line the phrase count uses (8 dB)
            pick = 0                # the lead-in has singing: it can't be part of the intro, the song follows the intro whole
    steady = info.get("drift", 0.0) <= 0.012   # drums from elsewhere in the song only lock on a steady tempo
    gen = n_bars - pick                                  # bars we make; the song's pickup fills the rest
    bt = np.append(_snap_bars(orig, bars), len(orig) / SR)
    seg = lambda x, i, j: x[int(round(bt[i] * SR)):int(round(bt[j] * SR))]
    half = n_bars // 2
    first = info.get("anchor", sb)
    clean = [i for i in range(first, nb - n_bars + 1, 8)                  # on the song's 8-bar phrase lines
             if voc[i:i + n_bars].mean() < 0.15 * v90 and beat[i:i + n_bars].mean() >= 0.75 * full
             and voc[i:i + n_bars].max() < 0.35 * v90]
    # does the song open much quieter than the intro material (a soft guitar intro, a spoken opening)?
    every = info.get("every")
    drm = info.get("drm")
    lvl = lambda i, n: 20 * np.log10(float(np.mean(every[max(0, i):max(0, i) + n])) + 1e-9) if every is not None else 0.0
    drums = None
    # a source stretch must have the beat in every bar (no drop-outs or stops inside the intro)
    dref = float(np.median(drm[drm > 0])) if drm is not None and np.any(drm > 0) else 0.0
    avoid = set(info.get("avoid", set()))     # source stretches already tried that left a gap in the intro
    if REDO_TRIED:                            # stretches where every version was already heard and turned down
        from collections import Counter
        per = Counter(int(k.split(":")[0]) for k in REDO_TRIED)
        avoid |= {b for b, n in per.items() if n >= (3 if n_bars >= 8 else 1)}
    solid = lambda i: i not in avoid and (drm is None or float(drm[i:i + n_bars].min()) >= 0.35 * dref)
    clean = [i for i in clean if solid(i)]
    if every is not None:                     # a full part of the song: within 3 dB of the loud section
        clean = [i for i in clean if lvl(i, n_bars) >= lvl(info["loud"], 8) - 3.0]
    src = min(clean, key=lambda i: abs(i - info["loud"])) if clean else info["loud"]
    if not clean and not solid(src):
        alts = [i for i in range(first, nb - n_bars + 1, 8) if solid(i)
                and float(np.mean(beat[i:i + n_bars])) >= 0.6 * full]
        if alts:
            src = min(alts, key=lambda i: abs(i - info["loud"]))
    if not clean and src + n_bars > nb:
        src = max(first, nb - n_bars)
    # a soft opening (6 dB under the intro's material). The DJ pools cut openings more often (about 4 dB
    # under), but that changed intros Vinny had approved (MTBD, Your Idol), so the line stays at 6 dB
    quiet_open = every is not None and lvl(sb, 4) < lvl(src, n_bars) - 6.0
    forced = info.get("force_entry")
    if forced:                               # second pass: the song comes in at its beat, past the quiet opening
        quiet_open, pick = False, 0
        sb = forced
    # a song that comes in thinner than the intro's material (MTBD comes in on voice and light drums with no
    # 808, Butter on bass and snaps): an intro made from the loud part builds up and then falls away when the
    # song starts. The intro keeps its build, but no tension drop (its roll and gap only make the drop
    # bigger), and over its last two bars the low end is taken down to what the song comes in with, the way
    # a DJ EQs the bass out before a mix, so the song takes over with no sudden drop
    rms = lambda i, n: float(np.sqrt(np.mean(seg(orig, i, min(i + n, nb)) ** 2))) if i < nb else 0.0
    lo_bars = _low_bars(orig, bt)
    lo = lambda i, n: float(np.mean(lo_bars[i:min(i + n, nb)])) if i < nb else 0.0
    thin = bool(not quiet_open and rms(sb, 2) > 0
                and (rms(sb, 2) < 0.75 * rms(src, n_bars) or lo(sb, 2) < 0.5 * lo(src, n_bars)))
    # the tension drop (kick out, roll, a silent last 16th) only pays off when the song comes in on a big hit:
    # its drums in the first two bars nearly as strong as at the drop. A song that comes in on a sung hook or
    # a lighter groove (salsa, bachata...) keeps the groove going straight into it instead.
    if os.environ.get("DJ_DEBUG"):
        _lo = _low_bars(orig, bt)
        print("DEBUG sb", sb, "loud", info["loud"], "src", src, "quiet_open", quiet_open)
        for i in range(0, min(nb, 20)):
            print(f"  bar {i:2d} drm {drm[i]:.4f} every {every[i]:.4f} voc {voc[i]:.4f} low {_lo[i]:.4f}")
        L = info["loud"]; print("  loud drm", float(np.mean(drm[L:L+8])), "low", float(np.mean(_lo[L:L+8])))
    # the tension drop (roll, riser, a silent 16th) is a club move: on a softer, slower song (a ballad, a
    # Christmas song like Mistletoe) it feels too intense, so only hip hop / dance music or songs at 100+ BPM get it
    calm = info.get("top", "") not in {"Hip Hop", "House", "Dance & Electronic", "Club"} and float(info.get("bpm") or 0) < 100
    big_entry = (not calm and not thin and drm is not None
                 and float(np.mean(drm[sb:sb + 2])) >= 0.7 * float(np.mean(drm[info["loud"]:info["loud"] + 8])))
    carry = pick if steady else 0
    own = None
    if quiet_open and drm is not None:
        L = info["loud"]
        ref = float(np.median(drm[L:L + 8])) + 1e-9
        own = next((j for j in range(sb, min(sb + 17, nb)) if drm[j] >= 0.4 * ref), None)
    if quiet_open and drm is not None and steady:
        # keep the intro's beat going under the soft opening until the song's own drums come in (2-8 bars)
        # measured against the whole loud section (one bar of it can be a stop with no drums), and carried
        # in phrase-sized pieces (8, 4 or 2 bars) that end before the song's own drums come in, faded out
        # across the last bar: a beat under the talking or singing, then it drops out into the song
        L = info["loud"]
        ref = float(np.median(drm[L:L + 8])) + 1e-9
        span = (own if own is not None else sb + 16) - sb
        carry = max(pick, 8 if span >= 8 else 4 if span >= 4 else 2 if span >= 2 else 0)
    if not quiet_open and not forced and drm is not None and steady:
        # the song comes in on a bar or few with no drums (piano and voice, like We Belong Together) before its
        # own beat starts: the intro's drums keep playing under those bars, so the beat never drops out
        L = info["loud"]
        ref = float(np.median(drm[L:L + 8])) + 1e-9
        # only bars with really no drums (a lighter beat of the song's own would clash: two snares at once)
        own2 = next((j for j in range(sb, min(sb + 5, nb)) if drm[j] >= 0.15 * ref), None)
        if own2 is not None and own2 > sb:
            carry = max(carry, own2 - sb)
            info["_dropout_fill"] = own2 - sb
    need_to = src + n_bars + max(0, carry - pick)              # bars of drums we may need from the source
    if clean:
        parts = [seg(orig, i, i + 1) for i in range(src, src + gen)]
        style, cue = "original instrumental", "Bar 5" if n_bars == 8 else "Halfway"
        if n_bars >= 16 and gen >= 8:
            hb = hq_stems(orig, bt[src], bt[src + 8])
            if hb is not None:
                for j in range(8):
                    nd_ = seg(hb["inst"], src + j, src + j + 1) - seg(hb["drums"], src + j, src + j + 1)
                    k_ = min(len(parts[j]), len(nd_))
                    parts[j] = parts[j].astype("float32").copy()
                    parts[j][:k_] -= _lowpass(nd_[:k_].astype("float32"), 220.0)
                style += ", bass held back until Halfway"
        # (no tension bar: the song's own bars run straight into it)
        if carry:
            hq = hq_stems(orig, bt[src + gen], bt[min(need_to, nb)])
            drums = hq["drums"] if hq is not None else stems["drums"]
    else:
        hq = hq_stems(orig, bt[src], bt[min(need_to, nb)])
        if hq is not None:
            inst, drums = hq["inst"], hq["drums"]
            tag = " (studio stems)"
        else:
            inst = orig[:len(stems["vocals"])] - stems["vocals"][:len(orig)]
            drums = stems["drums"]
            tag = ""
        # would the intro's instrumental play the same music the song opens with? (a song that is one loop,
        # like Macarena) Then the melody waits for the song: the intro is the beat alone.
        k = min(half, gen)
        rep = False
        if gen > half and sb + k < nb:
            C = bar_chroma(orig, bt)
            o = C[sb:sb + k]
            m = C[src + half:src + half + k]
            n_ = min(len(o), len(m))
            rep = n_ > 0 and float(np.mean([float(o[j] @ m[j]) for j in range(n_)])) >= 0.97
        # DJ-pool build, layer by layer on the song's own bars: kick only -> full drums -> + bass ->
        # + the music opening up through a filter -> a tension bar (kick out, snare roll, riser, last beat
        # silent) -> the song hits. A soft opening gets the gentle version (no melody, no tension drop).
        if rep and not quiet_open and not forced and info.get("vox_share", 0) >= 0.6:
            # the song opens straight on its sung hook, and its music loops on that same melody (salsa coros,
            # anthems like Vivir Mi Vida): any intro made from it either plays the hook early or is bare
            # percussion. DJs cut these in on beat 1, so no intro version
            raise OpensOnHook()
        # a soft opening: Afro / Caribbean / Latin pools play the beat under it (the soft style); in hip hop,
        # pop, R&B and the rest the pools skip it or bring the full beat (measured: 7 of 7 there, where the
        # drums-only style came out 10-50 dB too thin), so those get the full build
        mode = "soft" if quiet_open and info.get("layer", True) else "full"
        tension = False      # no snare-roll / riser build: it was used too often and sounded forced. DJ-pool intros run the beat straight into the song
        # a few versions from the same stretch of the song, and the one that scores best is kept: the 4-bar
        # loop from its first or second half, or the 8 bars played straight through
        options = {"loop A": [src + (j % 4) for j in range(gen)]} if gen >= 8 else {"straight": list(range(src, src + gen))}
        if gen >= 8 and src + 8 <= min(need_to, nb):
            options["loop B"] = [src + 4 + (j % 4) for j in range(gen)]
            options["straight"] = [src + (j % 8) for j in range(gen)]
        if REDO_TRIED:
            left = {k: v for k, v in options.items() if f"{src}:{k}" not in REDO_TRIED}
            options = left or options
        C_all = bar_chroma(orig, bt) if len(options) > 1 else None
        built = []
        for name_, loop in options.items():
            parts_ = build_layers(drums, inst, [seg(drums, i, i + 1) for i in loop],
                                  [seg(inst, i, i + 1) for i in loop], mode, tension,
                                  bass_from=8 if n_bars >= 16 else 0, dance=bool(info.get("dance")))
            if mode != "soft":
                parts_ = [_tilt(p_) for p_ in parts_]
            cost_, why_ = _intro_cost(parts_, loop, drums, seg, C_all, orig, bt, nb, sb,
                                      inst=inst if mode != "soft" else None, vocals=_vocals_of(stems))
            t_ = _taste()          # learned from your 👍 / 👎: the genre first, else across everything
            pref = t_.get((info.get("top", ""), name_), t_.get(("*", name_), 0))
            cost_ -= 0.08 * max(-5, min(5, pref))
            built.append((cost_, name_, parts_, why_))
        built.sort(key=lambda b: b[0])
        if REDO_SKIP_BEST and len(built) > 1:
            REDO_TRIED.add(f"{src}:{built[0][1]}")     # the first intro was (almost surely) this one
            built = built[1:] + built[:1]
        parts = built[0][2]
        global REDO_PICK
        REDO_PICK = f"{src}:{built[0][1]}"
        if len(built) > 1:
            log(f"intro versions: " + "; ".join(f"{b[1]} {b[0]:.2f} ({b[3]})" for b in built))
        style = {"soft": "drums into the soft opening",
                 "full": "the song's beat and melody"}[mode] + (", tension bar" if tension else "") + tag
        if len(built) > 1:
            style += f", best of {len(built)} versions ({built[0][1]})"
        cue = "Beat In"
    lens = [len(p) for p in parts]
    gen_bars = np.concatenate([[0], np.cumsum(lens)[:-1]]) / SR
    intro = np.clip(_xjoin(parts), -1, 1).astype("float32")
    # no gaps: a beat of near silence inside the intro (the source bar had a stop, like a drop that comes in
    # on beat 2) sounds like the track cut out. Build it again from another stretch of the song
    bl = [int(round((bt[min(src + j, nb)] - bt[min(src + j - 1, nb)]) * SR / 4)) for j in range(1, 2)][0]
    if bl > 0 and len(avoid) < 3:
        beats_ = [intro[k:k + bl] for k in range(0, len(intro) - bl + 1, bl)]
        lv_ = np.array([float(np.sqrt(np.mean(b ** 2))) for b in beats_]) if beats_ else np.zeros(0)
        if len(lv_) and float(lv_.min()) < 0.1 * float(np.median(lv_)):
            log(f"intro had a gap (source bar {src}); trying another part of the song")
            return build_intro_bars(orig, stems, bars, dict(info, avoid=set(avoid) | {src}), n_bars)
    def song_from(s0):
        x = orig[int(round(bt[s0] * SR)):].copy()
        if info.get("end"):                  # music-video rip: drop the talking or skit after the song too
            stop = int(round((info["end"] + 1.0 - bt[s0]) * SR))
            if 0 < stop < len(x):
                x = x[:stop].copy()
                f = min(int(1.5 * SR), len(x))
                x[-f:] *= np.linspace(1, 0, f, dtype="float32")[:, None]
        return x
    song = song_from(sb)
    info["song_from"] = sb
    beat_len = lambda i: int(round((bt[i + 1] - bt[i]) * SR / 4))
    fit = None
    if drums is not None and carry and quiet_open and carry > pick and info.get("layer", True):
        # a beat under a soft opening has to FIT it, not just sit on the grid: a steady 2-bar loop of the song's
        # own drum hits (pitched sound filtered out), the loop whose hits line up best with the opening's own
        # rhythm. Tried as full drums, then hi-hats / shakers only (no pitch at all); carried only if it fits.
        at_ = lambda j: int(round((bt[sb + j] - bt[sb]) * SR))
        opening = [song[at_(j):at_(j + 1)] for j in range(carry) if sb + j + 1 < len(bt)]
        P_open = [_hit_profile(b) for b in opening]
        C_open = _pitch_mix(np.concatenate(opening)) if opening else np.zeros(12)
        base, top = src + gen, min(need_to, nb)
        best = None
        for c in range(src, top - 1):
            if (c - base) % 2:
                continue                             # keep the 2-bar pattern (clave, dembow) in phase
            loop = _percussive(seg(drums, c, c + 2))
            cut = int(round((bt[c + 1] - bt[c]) * SR))
            for kind, lb in (("beat", [loop[:cut], loop[cut:]]),
                             ("hi-hats", [_highpass(loop[:cut], 5000.0), _highpass(loop[cut:], 5000.0)])):
                match = float(np.mean([_corr(_hit_profile(lb[j % 2]), P_open[j]) for j in range(len(P_open))]))
                pitch = float(np.minimum(_pitch_mix(np.concatenate(lb)), C_open).sum()) if kind == "beat" else 1.0
                ok = match >= CARRY_RHYTHM and pitch >= CARRY_PITCH
                if CARRY_DEBUG is not None:
                    CARRY_DEBUG.append(dict(c=c, kind=kind, lb=lb, opening=opening, match=match, pitch=pitch))
                key = (ok, kind == "beat", match + 0.5 * pitch)
                if best is None or key > best[0]:
                    best = (key, kind, lb, match, pitch, c)
        if best is not None:
            fit = f"rhythm {best[3]:+.2f}, pitch {best[4]:.2f}"
        if best is None or not best[0][0]:
            carry = pick
            style += f", beat not carried (didn't fit the opening: {fit})" if fit else ""
        else:
            _, kind, lb, _, _, c = best
            for j in range(carry):
                if sb + j + 1 >= len(bt):
                    break
                L = at_(j + 1) - at_(j)
                d = lb[j % 2][:L]
                if len(d) < L:
                    d = np.concatenate([d, np.zeros((L - len(d), d.shape[1]), "float32")])
                d = d.copy()
                if j == carry - 1:
                    d *= np.linspace(1, 0, len(d), dtype="float32")[:, None]
                n = min(len(d), len(song) - at_(j))
                if n > 0:
                    song[at_(j):at_(j) + n] += d[:n]
            song = np.clip(song, -1, 1)
            style += f", {'beat' if kind == 'beat' else 'hi-hats'} carried into the song (fits: {fit})"
            carry = 0                                # done
    if quiet_open and own is not None and sb < own <= sb + 16 and "carried into the song" not in style:
        # what DJ pools do with a weak opening nothing fits over (measured on 18 pool intros against their
        # originals: Needed Me, Luther, Lady, Ain't Nobody, Die With A Smile, Monaco...): the intro hands
        # straight over to the bar where the song's own beat comes in; the quiet opening is left out
        # come in on the song's phrase line: the beat often enters one bar after it (that bar is the lead-in
        # fill), so the intro hands over on the phrase line and the song's 8-bar phrases stay in line
        line = next((q for q in (own, own - 1) if q > sb and (q - anchor) % 4 == 0), own)
        # built again for that entry: the full build (kick, drums, bass, music) into the song's beat, not the
        # gentle one meant for a soft opening
        out = build_intro_bars(orig, stems, bars, dict(info, force_entry=line), n_bars)
        info["song_from"] = line
        return out[0], out[1], out[2] + f", song comes in where its beat starts ({line - info['sb']} quiet bars left out)", out[3], out[4]
    if drums is not None and carry:
        # a pickup bar (no singing) before the first hit: the intro's drums keep going under it and sit out
        # the last beat so the first hit lands clean
        for j in range(carry):
            k = src + gen + j
            if k + 1 > nb:
                break
            d = seg(drums, k, k + 1).copy()
            if j == carry - 1:
                q = beat_len(k)
                d[-q:] *= np.linspace(1, 0, q, dtype="float32")[:, None] ** 2
            at = int(round((bt[sb + j] - bt[sb]) * SR))
            n = min(len(d), len(song) - at)
            if n > 0:
                song[at:at + n] += d[:n]
        song = np.clip(song, -1, 1)
    elif drums is not None and style.startswith("drums") and "tension" not in style:   # drums sit out the last beat
        last = lens[-1] // 4
        d = seg(drums, src + gen - 1, src + gen)[-last:]
        intro[-len(d):] -= d * np.linspace(0.3, 1, len(d), dtype="float32")[:, None]
    # level: every intro plays as loud as the song it leads into (the bars the song comes in with), half a dB
    # under at the very start; the build comes from the music opening up, not from the volume. No dips (the
    # tension bar included), no jumps. Original-audio intros also open up from a muffled filter.
    if lens:
        if False and style.startswith("original") and half:      # no muffled filter opening (sounded like a cheap sweep)
            h = sum(lens[:half])
            dark = _lowpass(intro[:h], 900.0)
            w = np.linspace(0, 1, h, dtype="float32")[:, None] ** 1.5
            intro[:h] = dark * (1 - w) + intro[:h] * w
        rms = lambda x: float(np.sqrt(np.mean(x ** 2))) + 1e-9
        if len(song):
            s0 = info["song_from"]
            # the level the song comes in with: its first 4 bars (a quiet lead-in or fill bar alone would pull
            # the whole intro down)
            cut = lambda j: int(round((bt[min(s0 + j, len(bt) - 1)] - bt[s0]) * SR))
            first4 = [song[cut(j):cut(j + 1)] for j in range(4) if cut(j + 1) > cut(j)]
            entry = 20 * np.log10(float(np.median([rms(b) for b in first4])) if first4 else rms(song))
            starts = np.concatenate([[0], np.cumsum(lens)])
            n = len(lens)
            gains = []
            for i in range(n):
                part = intro[starts[i]:starts[i + 1]]
                body = part[:max(1, len(part) * 15 // 16)]        # ignore the silent 16th of a tension bar
                target = entry - 0.5 * (1 - i / max(1, n - 1))     # as loud as the song (the build is in the music)
                gains.append(float(np.clip(target - 20 * np.log10(rms(body)), -12.0, 10.0)))
            env = np.ones(len(intro), "float32")
            ramp = int(0.01 * SR)
            prev = gains[0]
            for i in range(n):
                a, e = starts[i], starts[i + 1]
                g0, g1 = 10 ** (prev / 20), 10 ** (gains[i] / 20)
                env[a:e] = g1
                r = min(ramp, e - a)
                env[a:a + r] = np.linspace(g0, g1, r, dtype="float32")
                prev = gains[i]
            intro = _limit(intro * env[:, None])
            if thin and n >= 2:
                a = int(starts[n - 2])
                tail = intro[a:]
                lo_t = float(np.sqrt(np.mean((tail - _highpass(tail, 150.0)) ** 2))) + 1e-9
                lo_s = float(np.sqrt(np.mean((song[:cut(2)] - _highpass(song[:cut(2)], 150.0)) ** 2))) if cut(2) > 0 else 0.0
                r = min(1.0, lo_s / lo_t)
                if info.get("top") == "Pop":
                    r = max(r, 0.7)       # pools keep more bass and body into a pop song's handover
                if r < 0.8:
                    hp = _highpass(tail, 150.0)
                    w = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, len(tail), dtype="float32")))[:, None]
                    eased = hp + (tail - hp) * (1 - (1 - r) * w)
                    # keep the build going: what's left (drums, music) comes up to make up the level, so the
                    # intro still ends 1 dB under the song instead of dipping before it
                    want = 10 ** (entry / 20)
                    last = eased[int(starts[n - 1]) - a:]
                    have = float(np.sqrt(np.mean(last[:max(1, len(last) * 15 // 16)] ** 2))) + 1e-9
                    g = float(np.clip(want / have, 1.0, 2.0))
                    intro[a:] = _limit(eased * (1 + (g - 1) * w))
                    style += ", bass eased out to meet the song"
        style += ", builds into the song"
    if forced and forced - 1 >= info["sb"] and len(intro):
        # the song was brought in past its quiet opening: the bar just before keeps playing under the end of
        # the intro (faded in), so words that start before the downbeat (a pickup) aren't cut off
        lead = orig[int(round(bt[forced - 1] * SR)):int(round(bt[forced] * SR))].astype("float32")
        n = min(len(lead), len(intro))
        if n:
            ramp = np.linspace(0, 1, n, dtype="float32")[:, None] ** 2
            intro[-n:] = _limit(intro[-n:] + lead[-n:] * ramp)
            style += ", pickup kept"
    # never a drop into the song: if the intro's last bar is still well over where the song comes in (a song
    # that enters quiet), the last two bars come down to just over the song's level, like a DJ riding the fader
    if len(gen_bars) >= 2 and len(song):
        a = int(round(float(gen_bars[-2]) * SR))
        a = max(0, min(a, len(intro) - 1))
        last = intro[-max(1, (len(intro) - a) // 2):]
        r_in = float(np.sqrt(np.mean(last ** 2))) + 1e-9
        head = song[:max(1, (len(intro) - a) // 2)].astype("float32")
        r_song = float(np.sqrt(np.mean(head ** 2))) + 1e-9
        over = 20 * np.log10(r_in / r_song)
        if over > 6.0:
            k = 10 ** (-(over - 1.0) / 20)
            w = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, len(intro) - a, dtype="float32")))[:, None]
            intro[a:] = intro[a:] * (1 - (1 - k) * w)
            style += ", faded to meet a quiet song entry"
    if info.pop("_dropout_fill", 0) and "carried into the song" not in style:
        style += ", beat kept going until the song's own drums come in"
    if HOOK_TEASE:
        try:
            teased, why = hook_tease(intro, lens, orig, stems, bt, info, style, sb)
            if why.startswith("bars"):
                intro, style = teased, style + f", hook teased ({why})"
            else:
                log(f"hook tease skipped: {why}")
        except Exception:
            log_exc("hook tease")
    song = _fill_gaps(intro, song, parts, lens)
    if getattr(song, "filled", False):
        style += ", silent gap after the song comes in filled with the intro's beat"
    # beat 1 starts clean, like the start of a record: the first hit's attack is found (it can start a few ms
    # before or after the bar line) and the intro starts 3 ms before it, from silence. Whatever was still
    # ringing from the bar before (an 808 tail, a held chord) is left out, so nothing sounds cut off or
    # smeared before the 1. Every bar and cue moves with it
    pre_n = int(0.030 * SR)
    a0 = int(round(bt[src] * SR))
    b1 = int(lens[0]) if len(lens) > 1 else 0
    if len(intro) > int(0.06 * SR) and (b1 >= pre_n or a0 >= pre_n):
        # what comes just before beat 1 is taken from the intro itself, just before its own bar 2 (the same
        # beat, so a hit that starts a little ahead of the bar line is heard whole). The intro's bars can come
        # from other parts of the song (or studio stems), so the song's own audio before this bar may not match
        if b1 >= pre_n:
            pre = intro[b1 - pre_n:b1].astype("float32")
        else:
            pre = orig[a0 - pre_n:a0].astype("float32")
        if pre.shape[1] != intro.shape[1]:
            pre = np.repeat(pre[:, :1], intro.shape[1], axis=1)
        w = np.concatenate([pre, intro[:int(0.040 * SR)]])
        m = np.abs(w).max(axis=1)
        h = int(0.001 * SR)
        env = np.array([m[k:k + h].max() for k in range(0, len(m) - h, h)])
        rise = np.diff(env, prepend=env[0])
        lo_, hi_ = 5, min(len(rise), 60)                       # look 25 ms before to 30 ms after the bar line
        att = int(np.argmax(rise[lo_:hi_])) + lo_ if hi_ > lo_ else 30
        if rise[att] > 0.15 * float(env.max() + 1e-9):
            start = max(0, att * h - int(0.003 * SR))           # 3 ms before the attack
            lead = pre_n - start                                # > 0: begins in the bar before; < 0: trims
            if lead > 0:
                intro = np.concatenate([w[start:pre_n], intro])
            else:
                intro = intro[-lead:]
            f = int(0.002 * SR)
            intro[:f] *= np.linspace(0, 1, f, dtype="float32")[:, None]
            gen_bars = gen_bars + lead / SR
            lens = [lens[0] + lead] + list(lens[1:])
    # no jump into the song: the intro's beat, carried on at its own tempo, has to land on the song's first
    # hits. If the song's hits sit a little early or late against that (a grid line a few ms or half a beat
    # off where the song comes in), the song's start is moved to match, so the beat runs straight through
    lock = 0
    try:
        beat = int(round(sum(lens[-4:]) / (4 * min(4, len(lens)))))  if lens else 0
        if beat > 0 and len(song) > 8 * beat:
            def pulse(x):
                m = x.mean(axis=1) if x.ndim == 2 else x
                h = int(0.005 * SR)
                e = np.sqrt(np.convolve(m ** 2, np.ones(h) / h, "same"))[::h]
                return np.maximum(0.0, np.diff(e, prepend=e[0]))
            s_on = pulse(song[:8 * beat + beat])
            i_on = pulse(intro[-4 * beat:])
            def score(d):                        # d: song samples to skip (+) or add in front (-)
                ks = [(k * beat + d) // int(0.005 * SR) for k in range(8)]
                return float(np.mean([s_on[max(0, j - 2):j + 3].max() for j in ks if 0 <= j < len(s_on) - 3] or [0]))
            ref = float(np.mean([i_on[max(0, (k * beat) // int(0.005 * SR) - 2):(k * beat) // int(0.005 * SR) + 3].max()
                                 for k in range(4)]))
            step = int(0.002 * SR)
            # only small slips (up to 30 ms). A bigger "better fit" is the off-beat bass or hats of house and
            # garage, not the song's real beat: moving that far put songs a third of a beat off (Bad Wolf)
            lim = int(0.030 * SR)
            cands = range(-lim, lim + 1, step)
            best = max(cands, key=score)
            if abs(best) > int(0.012 * SR) and score(best) > 1.3 * score(0) and score(best) > 0.3 * ref:
                lock = best
    except Exception:
        log_exc("handover lock")
    if lock > 0:
        song = song[lock:]
        style += f", song start moved {lock * 1000 // SR} ms to stay on the beat"
    elif lock < 0:
        a1 = int(round(bt[info.get("song_from", info["sb"])] * SR))
        lead_in = orig[max(0, a1 + lock):a1].astype("float32")
        if lead_in.shape[1] != song.shape[1]:
            lead_in = np.repeat(lead_in[:, :1], song.shape[1], axis=1)
        song = np.concatenate([lead_in, song.astype("float32")])
        style += f", song start moved {-lock * 1000 // SR} ms earlier to stay on the beat"
    audio = _xjoin([intro, song.astype("float32")])
    return audio, gen_bars, style, cue, sum(lens) / SR - lock / SR


# ------------------------------------------------------------------ structure
def analyse(stems, first, bpm, n_samples):
    """Times (seconds) for Start, Vocals, Drop, Breakdown, Drop 2 and Outro, on whole bars and in song
    order. Works on 4-bar blocks: the drop is the first block that reaches the song's full energy, the
    breakdown the clearest dip after it, drop 2 where the energy comes back, the outro where the last
    vocals end (or the energy falls away for good)."""
    bar = 4 * 60.0 / bpm
    while first < 0:
        first += bar
    nbars = int((n_samples / SR - first) / bar)
    if nbars < 16:
        raise RuntimeError("too short to analyse")

    def per_bar(x):
        mono = x.mean(axis=1)
        out = np.zeros(nbars)
        for i in range(nbars):
            seg = mono[int((first + i * bar) * SR):int((first + (i + 1) * bar) * SR)]
            out[i] = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
        return out

    voc = per_bar(stems["vocals"])
    drm = per_bar(stems["drums"])
    beat = per_bar(stems["drums"] + stems["bass"] + stems["other"])
    every = per_bar(stems["drums"] + stems["bass"] + stems["other"] + stems["vocals"])
    t = lambda i: first + i * bar
    blk = lambda x, i, n=4: float(x[i:i + n].mean()) if i < nbars else 0.0
    res = {}
    sb = next((i for i in range(nbars) if every[i] > 0.1 * np.percentile(every, 90)), 0)
    res["Start"] = t(sb)
    vthr = 0.3 * np.percentile(voc, 90)
    vin = next((i for i in range(sb, nbars - 1) if voc[i] > vthr and voc[i + 1] > vthr), None)
    res["Vocals"] = t(vin) if vin is not None and np.percentile(voc, 90) > 0.01 else None

    # the song's "full" level: what its loudest quarter of 4-bar blocks reach
    starts = list(range(sb, nbars - 3, 4))
    levels = np.array([blk(beat, i) for i in starts])
    full = np.percentile(levels, 75) if len(levels) else 0
    last_voc = max([i for i in range(nbars) if voc[i] > vthr], default=None)
    # outro: 8-bar boundary after the last vocals, or where the energy falls away for good
    out = None
    if last_voc is not None:
        out = ((last_voc + 1 - sb + 7) // 8) * 8 + sb
    tail = [i for i in starts if blk(beat, i) >= 0.6 * full]
    if out is None or out > nbars - 8:
        out = (tail[-1] + 4) if tail else nbars - 8
    out = min(max(out, sb + 16), nbars - 4)
    # drop: first block (after the start) that reaches full energy with a real lift into it
    lift = lambda i: blk(beat, i) / (min(blk(beat, max(sb, i - 8)), blk(beat, i - 4)) + 1e-9)
    drop = next((i for i in starts if sb + 4 <= i < out - 8 and blk(beat, i) >= 0.85 * full and lift(i) > 1.1), None)
    if drop is None or drop > sb + 0.6 * (out - sb):
        # flat, loud masters (lots of rock): no clear lift, so take the biggest one in the first 60%
        cands = [i for i in starts if sb + 4 <= i <= sb + 0.6 * (out - sb)]
        drop = max(cands, key=lift, default=drop)
    res["Drop"] = t(drop) if drop is not None else None
    brk = drop2 = None
    if drop is not None:
        after = [i for i in starts if drop + 8 <= i <= out - 8]
        # clearest dip: the drums (or the whole beat) fall well below the drop
        dips = [i for i in after if blk(drm, i) < 0.5 * blk(drm, drop, 8) or blk(beat, i) < 0.65 * blk(beat, drop, 8)]
        if dips:
            brk = dips[0]
        elif after:
            brk = min(after, key=lambda i: blk(beat, i))     # no clear dip: the quietest stretch
        if brk is not None:
            drop2 = next((i for i in starts if brk + 4 <= i < out and blk(beat, i) >= 0.85 * full and lift(i) > 1.1), None)
    res["Breakdown"] = t(brk) if brk is not None else None
    res["Drop 2"] = t(drop2) if drop2 is not None else None
    res["Outro"] = t(out)
    loud = drop if drop is not None else max(starts, key=lambda i: blk(beat, i))
    # how mixable the song's own opening is: drums in its first 8 bars vs drums at the drop
    res["_intro_beat"] = blk(drm, sb, 8) / (blk(drm, loud, 8) + 1e-9)
    return res, loud, bar, first


def _lowpass(x, hz):
    """Gentle FFT low-pass (muffled 'filter closed' sound)."""
    f = np.fft.rfftfreq(len(x), 1 / SR)
    mask = 1 / (1 + (f / hz) ** 4)
    return np.fft.irfft(np.fft.rfft(x, axis=0) * mask[:, None], n=len(x), axis=0).astype("float32")






def retire_intro(f):
    """Move a DJ Grab intro that isn't needed any more into _To Delete (never deleted) and take it out
    of Serato's library and crates."""
    f = Path(f)
    vol = djgrab.volume_root(str(f))
    if vol is None:                          # on the Mac itself: a _To Delete folder next to the song
        dest = f.parent / "_To Delete" / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(f, dest)
        return
    vol = Path(vol)
    old_rel = str(f.resolve().relative_to(vol.resolve()))
    dest = vol / "_To Delete" / "DJ Grab intros not needed" / f.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(f, dest)
    if not PLAIN:                                               # the owner's Serato library only
        djgrab.purge_serato_refs(vol, old_rel)                  # gone from Serato's library and crates
        djgrab.purge_serato_refs(vol, "_To Delete/")           # (and anything else waiting in _To Delete)


def needs_intro(times, bar, dance=False):
    """DJ-pool rule: a song needs an intro version unless it already opens with 8 clean bars that you can
    mix over, meaning no vocals AND a real beat (drums at least ~40% as strong as at the drop)."""
    v, st = times.get("Vocals"), times.get("Start")
    if st is None:
        return False
    early_vocals = v is not None and (v - st) < 8 * bar - 0.25 * bar
    if early_vocals and "_vox_share" in times:
        # only ad-libs or quiet chants up front still mix fine: it takes a voice on top of the beat for
        # 40% of the opening, or a sung/rapped line holding two bars running
        # DJ pools still make an intro when ad-libs or chants sit over the opening beat (the intro is the
        # beat without them), so only a stray sound or two (under 10% of the opening) counts as clean
        early_vocals = times["_vox_share"] >= 0.1 or times["_vox_run"] >= 0.2
    weak_beat = times.get("_intro_beat", 1.0) < 0.4
    if dance:
        # house / dance music is made for DJs: original and extended mixes open on drums, percussion or hats
        # that build (often under the loud part's level), and vocal chops or chants over that beat are mixed
        # over all the time. DJ pools rarely make intro edits for them. So only a song that has no beat at all
        # to start, or a real sung / rapped line from the top, gets one
        early_vocals = v is not None and (v - st) < 8 * bar - 0.25 * bar and (
            times.get("_vox_share", 1.0) >= 0.4 or times.get("_vox_run", 1.0) >= 0.5)
        weak_beat = times.get("_intro_beat", 1.0) < 0.12
    return early_vocals or weak_beat


# ------------------------------------------------------------------ per song
_LOCKED = {}


def grid_locked(p):
    """True if you locked this song's beatgrid in Serato (then DJ Grab never changes it)."""
    import sqlite3
    vol = djgrab.volume_root(str(p))
    if vol is None:
        return False
    key = str(vol)
    if key not in _LOCKED:
        _LOCKED[key] = set()
        db = Path(vol) / "_Serato_" / "Library" / "location.sqlite"
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            _LOCKED[key] = {unicodedata.normalize("NFC", r[0]).lower()
                            for r in c.execute("select portable_id from asset where is_beatgrid_locked=1")}
            c.close()
        except Exception:
            pass
    rel = unicodedata.normalize("NFC", str(Path(p).resolve().relative_to(Path(vol).resolve()))).lower()
    try:                                  # locks DJ Grab set itself (to keep its own grids) don't count as yours
        ours = set(json.loads((djgrab.APP_SUPPORT / "locked_by_djgrab.json").read_text()))
    except Exception:
        ours = set()
    ours.add("vinny's usb/latin/tropical/los del rio - macarena.mp3")   # locked by DJ Grab on Nov 13
    return rel in _LOCKED[key] and rel not in ours


def genre_of(p):
    """Top genre folder of a song (helps tell 93 BPM hip hop from a 124 BPM misread)."""
    parts = Path(p).parts
    for i, x in enumerate(parts):
        if x in GENRE_CENTER:
            return x
    return None


ACAP_LABEL = re.compile(r"\s*\(Acap (?:In|Out|In-Out)\)", re.I)


def intro_path(p):
    """"Artist - Title (Intro).mp3" next to the song."""
    p = Path(p)
    return p.with_name(f"{p.stem} (Intro){p.suffix}")


def is_our_intro(f):
    try:
        from mutagen.id3 import ID3
        t = ID3(str(f))
        return bool(t.getall("TXXX:DJ Grab intro")) or any("DJ Grab intro" in str(c) for c in t.getall("COMM"))
    except Exception:
        return False


def acap_label(times):
    i, o = times.get("_acap_in"), times.get("_acap_out")
    return "Acap In-Out" if i and o else "Acap In" if i else "Acap Out" if o else None




def has_intro_version(p):
    """Another intro version already sits next to the song (ours don't count: they get rebuilt)."""
    p = Path(p)
    a, _, t = p.stem.partition(" - ")
    core = djgrab.song_key(a, t) if t else None
    for f in p.parent.iterdir():
        if f == p or f.name.startswith(".") or not f.name.lower().endswith(".mp3"):
            continue
        fa, _, ft = f.stem.partition(" - ")
        if core and djgrab.song_key(fa, ft) == core and "intro" in "".join(djgrab.version_sig(ft)) and not is_our_intro(f):
            return True
    return intro_path(p).exists() and not is_our_intro(intro_path(p))


def overview_bytes(audio):
    """Serato's little whole-track waveform (240 columns x 16 pixels), so Serato sees the file as analysed."""
    mono = np.abs(audio.mean(axis=1)) if audio.ndim == 2 else np.abs(audio)
    cols = np.array_split(mono, 240)
    amp = np.array([float(np.sqrt(np.mean(c * c))) if len(c) else 0.0 for c in cols])
    amp = amp / (amp.max() + 1e-9)
    out = bytearray(b"\x01\x05")
    for a in amp:
        h = 8 * a
        for r in range(16):
            d = abs(r - 7.5)
            out.append(int(40 + 180 * (1 - d / h)) if h > 0.5 and d < h else 1)
    return bytes(out)


def mark_analysed(path, src_id3, audio, bpm):
    """Give a new intro file the tags Serato writes after analysing a track, with OUR tempo. Without them
    Serato analyses the file itself on import and replaces the grid with its own rounded BPM (131.00 for a
    130.77 song), which drifts a beat off within a minute."""
    from mutagen.id3 import ID3, GEOB, TBPM
    id3 = ID3(str(path))
    ana = next((f.data for f in src_id3.getall("GEOB") if f.desc == "Serato Analysis"), b"\x02\x01")
    gain = b"0.000\x00"
    keep_trim = b"0.000"
    for f in src_id3.getall("GEOB"):
        if f.desc == "Serato Autotags":
            parts = f.data[2:].split(b"\x00")
            if len(parts) >= 2 and parts[1]:
                gain = parts[1] + b"\x00"
            if len(parts) >= 3 and parts[2]:
                keep_trim = parts[2]
    auto = b"\x01\x01" + f"{bpm:.2f}".encode() + b"\x00" + gain + keep_trim + b"\x00"
    for desc, data in (("Serato Analysis", ana), ("Serato Autotags", auto), ("Serato Overview", overview_bytes(audio))):
        id3.delall("GEOB:" + desc)
        id3.add(GEOB(encoding=0, mime="application/octet-stream", desc=desc, data=data))
    id3.setall("TBPM", [TBPM(encoding=3, text=f"{bpm:.2f}")])
    id3.save(str(path), v2_version=3)


GRID_WATCH = djgrab.APP_SUPPORT / "grid_watch.json"


def watch_grid(path):
    """Remember the grid we wrote, so if Serato ever replaces it, the next tidy-up (Serato closed) puts it back
    and locks it."""
    from mutagen.id3 import ID3
    try:
        g = next(f.data for f in ID3(str(path)).getall("GEOB") if f.desc == "Serato BeatGrid")
        w = json.loads(GRID_WATCH.read_text()) if GRID_WATCH.exists() else {}
        w[str(path)] = {"grid": base64.b64encode(g).decode(), "t": time.time()}
        tmp = GRID_WATCH.with_suffix(".tmp"); tmp.write_text(json.dumps(w)); tmp.replace(GRID_WATCH)
    except Exception as ex:
        log(f"watch_grid {path}: {ex}")


def check_intro(audio, ibars, offset, bpm, song=None):
    """Self-check every intro before it is kept: the seam into the song shouldn't jump in loudness, and the
    intro's hits must sit on its own grid. Problems are named in the result and the log for review."""
    flags = []
    bar = 4 * 60.0 / bpm
    lvl = lambda t0, t1: 20 * np.log10(float(np.sqrt(np.mean(audio[int(t0 * SR):int(t1 * SR)] ** 2))) + 1e-9)
    if offset + bar < len(audio) / SR:
        jump = lvl(offset, offset + bar / 2) - lvl(offset - bar / 2, offset)
        if abs(jump) > 9:
            flags.append(f"{'drop' if jump < 0 else 'jump'} of {abs(jump):.0f} dB into the song")
    env, low = onset_env(audio[:int((offset + 4 * bar) * SR)])
    fps = SR / HOP
    beats = np.arange(0, offset + 4 * bar - 0.01, bar / 4)
    sc = lambda sh: float(_interp(env, (beats + sh - ONSET_BIAS) * fps).mean())
    best = max(np.arange(-0.04, 0.0401, 0.002), key=sc)
    if abs(best) > 0.015 and sc(best) > 1.3 * sc(0.0):
        flags.append(f"hits {best * 1000:+.0f} ms off the grid")
    flags += quality_gate(audio, ibars, offset, bpm, song)
    return flags


def bar_jumps(audio, starts, beat, detail=False):
    """Where the beat jumps from one bar to the next: a bar whose hits sit clearly on one spot against the
    grid, followed by a bar whose hits sit clearly somewhere else (a loop seam or handover out of time).
    Two measures have to agree: the grid position of each bar's hits, and how the two bars line up with each
    other. Returns the bar numbers (0 = the first) that start out of time."""
    x = audio.mean(axis=1) if audio.ndim == 2 else audio
    h = 0.005; hp = int(h * SR)
    e = np.sqrt(np.convolve(np.asarray(x, "float32") ** 2, np.ones(hp, "float32") / hp, "same"))[::hp]
    o = np.maximum(0.0, np.diff(e, prepend=e[0]))
    n = int(round(4 * beat / h)); L = int(beat / 2 / h)
    ds = np.arange(-beat / 2, beat / 2, h)
    pos, segs = [], []
    for t0 in starts:
        i0 = int(round(t0 / h))
        if i0 < 0 or i0 + n + L + 2 >= len(o):
            break
        idx = np.clip(np.round((t0 + np.arange(4)[:, None] * beat + ds[None, :]) / h).astype(int), 1, len(o) - 2)
        sc = np.maximum(np.maximum(o[idx - 1], o[idx]), o[idx + 1]).sum(0)
        k = int(np.argmax(sc))
        hits = np.maximum(np.maximum(o[idx[:, k] - 1], o[idx[:, k]]), o[idx[:, k] + 1])
        steady = int((hits > 0.2 * hits.max()).sum()) >= 3          # a real beat, not a few stray sounds
        pos.append((ds[k], sc[k] / (np.median(sc) + 1e-12) if steady else 0.0))
        segs.append(o[i0:i0 + n])
    circ = lambda d: (d + beat / 2) % beat - beat / 2
    out = []
    prev = None                                  # the last bar with a clear, steady beat
    for k in range(len(pos)):
        if pos[k][1] < 3:
            continue
        if prev is not None:
            p0, p1 = pos[prev][0], pos[k][0]
            d = circ(p1 - p0)
            if abs(d) >= 0.09:
                a = segs[prev] - segs[prev].mean(); b = segs[k] - segs[k].mean()
                nm = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
                c = np.array([np.dot(a[max(0, -l):n - max(0, l)], b[max(0, l):n - max(0, -l)]) for l in range(-L, L + 1)]) / nm
                j = int(np.argmax(c)); lag = (j - L) * h
                nxt = next((m for m in range(k + 1, len(pos)) if pos[m][1] >= 1.5), None)   # the bar after, if it has a beat
                stays = nxt is None or abs(circ(pos[nxt][0] - p1)) < 0.045   # a one-bar fill that comes back is the song's own pattern
                if stays and abs(lag) > 0.05 and c[j] >= 0.15 and c[j] > 1.5 * max(c[L], 0.0) and abs(circ(d - lag)) < 0.06:
                    out.append(k)
        prev = k
    return (out, pos) if detail else out


def _onsets5(audio):
    x = audio.mean(axis=1) if audio.ndim == 2 else audio
    hp = int(0.005 * SR)
    e = np.sqrt(np.convolve(np.asarray(x, "float32") ** 2, np.ones(hp, "float32") / hp, "same"))[::hp]
    return np.maximum(0.0, np.diff(e, prepend=e[0]))


def _song_moved(song, moved):
    """The original song lined up with an intro file whose song part was slid by repair_intro."""
    if not moved or song is None:
        return song
    n = int(round(abs(moved) * SR))
    return song[n:] if moved > 0 else np.concatenate([np.zeros((n,) + song.shape[1:], song.dtype), song])


def repair_intro(audio, ibars, offset, bpm):
    """Fix an intro instead of throwing it away:
      beat jumps: each stretch of the intro whose hits sit off the song's beat is slid back onto it (the bars
        repeat, so what slides past one end comes back in from the other end of the same bar)
      missing hits: a beat whose hit is missing from one intro bar, while the other intro bars all have it,
        gets that hit back from the nearest bar that has it (the last bar is left alone: it can be the song's
        own pickup)
      the song coming in off the intro's beat: the song is slid onto it (see below)
    Returns (audio, what was fixed, how far the song moved in s). The result is checked again like any other intro."""
    beat = 60.0 / bpm; bar = 4 * beat
    ib = [float(t) for t in ibars if float(t) < offset - beat]
    if len(ib) < 2:
        return audio, [], 0.0
    starts = ib + [offset + k * bar for k in range(3)]
    jumps, pos = bar_jumps(audio, starts, beat, detail=True)
    a = np.array(audio, dtype="float32", copy=True)
    fixed = []
    nI = len(ib)
    circ = lambda d: (d + beat / 2) % beat - beat / 2
    xf = int(0.004 * SR)
    ref = None

    def put(i0, i1, new):
        """Write new over a[i0:i1] with short crossfades at both ends."""
        old = a[i0:i1].copy()
        a[i0:i1] = new
        for lo, w in ((0, np.linspace(0, 1, xf, dtype="float32")), (len(new) - xf, np.linspace(1, 0, xf, dtype="float32"))):
            if 0 <= lo and lo + xf <= len(new) and not (lo == 0 and i0 == 0):
                ww = w[:, None] if a.ndim == 2 else w
                a[i0 + lo:i0 + lo + xf] = new[lo:lo + xf] * ww + old[lo:lo + xf] * (1 - ww)

    if jumps:
        # the intro's first stretch sets the beat: its first hit was already placed on the attack, so the
        # later stretches are slid onto it. A jump only where the song comes in means the song's beat grid is
        # off: that isn't fixed here (sliding the intro would cut its first hit)
        cuts = [0] + [k for k in jumps if 0 < k < nI] + [nI]
        ps0 = [pos[k][0] for k in range(0, min(cuts[1], len(pos))) if pos[k][1] >= 3]
        ref = float(np.median(ps0)) if ps0 else None
        if ref is not None and len(cuts) > 2:
            for r0, r1 in list(zip(cuts[:-1], cuts[1:]))[1:]:
                ps = [pos[k][0] for k in range(r0, min(r1, len(pos))) if pos[k][1] >= 3]
                if not ps:
                    continue
                d = circ(float(np.median(ps)) - ref)
                if abs(d) < 0.02:
                    continue
                t0 = 0.0 if r0 == 0 else starts[r0]
                t1 = offset if r1 >= nI else starts[r1]
                i0, i1 = int(round(t0 * SR)), int(round(t1 * SR))
                nb_ = int(round(bar * SR))
                src = np.arange(i0, i1) + int(round(d * SR))
                src = np.where(src < i0, src + nb_, src)
                src = np.where(src >= i1, src - nb_, src)
                src = np.clip(src, 0, len(audio) - 1)
                put(i0, i1, np.asarray(audio, "float32")[src])
                fixed.append(f"bars {r0 + 1}-{r1} moved {-d * 1000:+.0f} ms onto the beat")

    # the song comes in off the intro's beat (its beat grid is off where it starts): the song is slid so its
    # hits land on the intro's beat. Song a little late: the quiet lead-in before its first hit is trimmed.
    # Song early: the intro's beat runs on for that moment before the song comes in. Only when the song's
    # first bars all agree on where their beat is.
    moved = 0.0
    if nI in jumps and ref is None:
        ps0 = [pos[k][0] for k in range(nI) if pos[k][1] >= 3]
        ref = float(np.median(ps0)) if ps0 else None
    if nI in jumps and ref is not None:
        sp = [pos[k] for k in range(nI, len(pos))]
        if len(sp) >= 2 and all(r >= 3 for _, r in sp[:2]) and abs(circ(sp[1][0] - sp[0][0])) < 0.03:
            d = circ(float(np.median([p for p, _ in sp[:3]])) - ref)
            # exact amount: line the song's first bar up against the intro's last full bar
            o_ = _onsets5(a); h_ = 0.005; n_ = int(round(bar / h_)); L_ = int(beat / 2 / h_)
            kb = max(k for k in range(nI) if pos[k][1] >= 3) if any(pos[k][1] >= 3 for k in range(nI)) else nI - 1
            x0 = o_[int(round(ib[kb] / h_)):int(round(ib[kb] / h_)) + n_]
            x1 = o_[int(round(offset / h_)):int(round(offset / h_)) + n_ + L_]
            if len(x0) == n_ and len(x1) == n_ + L_:
                lags = [l for l in range(-L_, L_ + 1) if 0 <= l and l + n_ <= len(x1)] or [0]
                sc_ = [float(np.dot(x0 - x0.mean(), x1[l:l + n_] - x1[l:l + n_].mean())) for l in lags]
                lg = lags[int(np.argmax(sc_))] * h_
                if abs(lg - d) < 0.06:
                    d = lg
            io, nd = int(round(offset * SR)), int(round(abs(d) * SR))
            nb_ = int(round(bar * SR))
            if d > 0.02:
                lead = float(np.sqrt(np.mean(a[io:io + max(1, nd - int(0.015 * SR))] ** 2)) + 1e-9)   # up to just before the hit
                body = float(np.sqrt(np.mean(a[io:io + nb_] ** 2)) + 1e-9)
                if lead < 0.25 * body:
                    a = np.concatenate([a[:io], a[io + nd:]])
                    moved = d
            elif d < -0.02 and io - nb_ >= 0:
                fill = a[io - nb_:io - nb_ + nd].copy()        # the start of a bar of the intro, carried on
                f_ = min(int(0.004 * SR), nd // 2)
                if f_ > 0:
                    w = np.linspace(1, 0, f_, dtype="float32")
                    fill[-f_:] *= w[:, None] if a.ndim == 2 else w
                a = np.concatenate([a[:io], fill, a[io:]])
                moved = d
            if moved:
                fixed.append(f"song brought in {abs(moved) * 1000:.0f} ms {'sooner' if moved > 0 else 'later'} to land on the intro's beat")

    # missing hits inside the intro
    o = _onsets5(a)
    conf = [p for p, r in pos[:nI] if r >= 3]
    ph = float(np.median(conf)) if conf else 0.0
    if fixed and ref is not None:
        ph = ref
    def hit(t):
        i = int(round(t / 0.005))
        return float(o[max(0, i - 5):i + 6].max()) if 0 <= i < len(o) else 0.0
    H = np.array([[hit(ib[j] + b * beat + ph) for b in range(4)] for j in range(nI)])
    if H.size and H.max() > 0:
        for b in range(4):
            col = H[:, b]
            for j in range(1, nI - 1):
                others = np.delete(col, j)
                m = float(np.median(others))
                if m < 0.3 * H.max() or col[j] >= 0.15 * m or (others < 0.5 * m).sum() > 1:
                    continue
                good = [g for g in sorted(range(nI), key=lambda g: abs(g - j)) if g != j and col[g] >= 0.6 * m]
                if not good:
                    continue
                g = good[0]
                ts, tg = ib[j] + b * beat + ph - 0.015, ib[g] + b * beat + ph - 0.015
                n_ = int(round(beat * SR))
                i0, ig = int(round(ts * SR)), int(round(tg * SR))
                if i0 < 0 or ig < 0 or i0 + n_ > int(offset * SR) or ig + n_ > len(a):
                    continue
                # only a real hole: the whole beat goes quiet, not a hit the pattern leaves out on purpose
                rms = lambda i: float(np.sqrt(np.mean(a[i:i + n_] ** 2)) + 1e-9)
                if rms(i0) > 0.35 * rms(ig):
                    continue
                if any(0 <= b2 < 4 and H[j, b2] < 0.6 * float(np.median(np.delete(H[:, b2], j))) for b2 in (b - 1, b + 1)):
                    continue                              # more than one beat gone: a planned stop, left alone
                put(i0, i0 + n_, a[ig:ig + n_].copy())
                fixed.append(f"missing hit on bar {j + 1} beat {b + 1} filled")
    return (a if fixed else audio), fixed, moved


SERIOUS = ("cut-off start", "beat jumps", "drums clash")


def quality_gate(audio, ibars, offset, bpm, song=None):
    """The problems a DJ hears right away. An intro with any of them is built again another way, and if that
    still fails it isn't kept (the song is left as it is):
      cut-off start: sound before beat 1's own hit, or the hit starting already at full level
      beat jumps:    the intro's beat, carried on, doesn't land on the song's hits where it comes in
      drums clash:   hits in the song's first bars that the original song doesn't have, off its beat"""
    out = []
    beat = 60.0 / bpm
    try:                                   # every intro bar and the song's first 2 bars, each in time with the last
        st = [float(t) for t in list(ibars) if float(t) < offset - beat] + [offset + k * 4 * beat for k in range(3)]
        jb, pos = bar_jumps(audio, st, beat, detail=True)
        if jb:
            out.append("beat jumps")
        # the intro's hits sit clearly ahead of its bar lines: beat 1 started before the file does (cut off)
        p01 = [p for p, r in pos[:2] if r >= 3]
        if len(p01) == 2 and -0.045 < min(p01) and max(p01) < -0.010:     # (further off is the off-beat, not beat 1)
            out.append("cut-off start")
    except Exception:
        pass
    h = int(0.001 * SR)
    m = np.abs(audio[:int(0.06 * SR)]).max(axis=1)
    env = np.array([m[k:k + h].max() for k in range(0, len(m) - h, h)])
    if len(env) > 10 and "cut-off start" not in out:
        att = int(np.argmax(np.diff(env, prepend=env[0])))
        if env[:max(1, att - 4)].max() > 0.35 * env.max() or env[0] > 0.5 * env.max():
            out.append("cut-off start")
    def onsets(x):
        mono = x.mean(axis=1) if x.ndim == 2 else x
        hp = int(0.005 * SR)
        e = np.sqrt(np.convolve(mono ** 2, np.ones(hp) / hp, "same"))[::hp]
        return np.maximum(0.0, np.diff(e, prepend=e[0]))
    o0 = int(offset * SR)
    if 0 < o0 < len(audio) - int(8 * beat * SR):
        seg = onsets(audio[o0:o0 + int(8 * beat * SR)])
        at = lambda t: seg[max(0, int(t / 0.005) - 2):int(t / 0.005) + 3].max() if int(t / 0.005) < len(seg) else 0.0
        on = np.mean([at(k * beat) for k in range(8)])
        off = max(np.mean([at(k * beat + d) for k in range(8)]) for d in (0.02, 0.03, -0.02, -0.03))
        if off > 1.4 * on and off > 0 and "beat jumps" not in out:
            out.append("beat jumps")
        if song is not None and len(song) >= int(16 * beat * SR):
            a = onsets(audio[o0:o0 + int(16 * beat * SR)])
            b0 = onsets(np.asarray(song[:int(16 * beat * SR) + int(0.05 * SR)], dtype="float32"))
            # line the original up with the intro file's song part first (its start can sit a few ms off:
            # bar lines snapped to the attack, the song start nudged onto the beat). Only hits still left
            # over once they match up are extra
            def extra_share(lag):
                b = np.concatenate([np.zeros(-lag), b0])[:len(a)] if lag < 0 else b0[lag:lag + len(a)]
                n = min(len(a), len(b))
                ex = np.maximum(0.0, a[:n] - 2.0 * np.maximum(b[:n], np.roll(b[:n], 1)) - 0.02 * float(a[:n].max() + 1e-9))
                return float(ex.sum()) / float(a[:n].sum() + 1e-9)
            if min(extra_share(l) for l in range(-8, 9)) > 0.25:          # within 40 ms either way
                out.append("drums clash")
    return out


# ------------------------------------------------------------------ tempo fix (songs a hair off a whole BPM)
TEMPO_TAG = "DJ Grab tempo"
_RUBBERBAND = None


def _has_rubberband():
    global _RUBBERBAND
    if _RUBBERBAND is None:
        try:
            r = subprocess.run([djgrab.FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True)
            _RUBBERBAND = " rubberband " in r.stdout
        except Exception:
            _RUBBERBAND = False
    return _RUBBERBAND


_PB = None


def _ensure_pedalboard():
    """Spotify's pedalboard: bundles the Rubber Band time-stretcher (about 5 MB), installed once."""
    global _PB
    if _PB is None:
        try:
            import pedalboard  # noqa: F401
            _PB = True
        except Exception:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pedalboard"], capture_output=True, text=True)
            _PB = r.returncode == 0
            if not _PB:
                log("pedalboard install failed: " + r.stderr[-300:])
    return _PB


def retempo(path, from_bpm, to_bpm):
    """Make a song play at exactly to_bpm: time-stretch it (pitch unchanged) by a fraction of a percent,
    re-encode at 320 kbps, keep every tag, move your own cues and loops to match, and keep the untouched
    original in _To Delete (never deleted). Returns True when the file was replaced."""
    from mutagen.id3 import ID3, TXXX, TBPM, GEOB
    path = Path(path)
    r = to_bpm / from_bpm
    vol = djgrab.volume_root(str(path))
    if vol is not None and shutil.disk_usage(vol).free < 2e9:
        log(f"{path.name}: tempo fix skipped, less than 2 GB free on the drive")
        return False
    tmp = path.with_name("." + path.stem + ".djgrab-tempo.mp3")
    try:
        from mutagen.mp3 import MP3
        rate = int(MP3(str(path)).info.sample_rate)
    except Exception:
        rate = SR
    # a fraction-of-a-percent change: playing the audio that much faster (resampled, like a turntable) is
    # cleaner than any time-stretch; the pitch moves under 5 cents, below what anyone can hear
    af = (f"asetrate={int(round(rate * r))},aresample={rate}:resampler=soxr:precision=28" if abs(r - 1) <= 0.003
          else f"rubberband=tempo={r:.7f}:pitchq=quality:transients=crisp" if _has_rubberband() else f"atempo={r:.7f}")
    src_in, src_af = str(path), af
    if abs(r - 1) > 0.003 and not _has_rubberband() and _ensure_pedalboard():
        # bigger nudges: Rubber Band (studio time-stretch, pitch kept), built into the app via pedalboard
        try:
            import pedalboard
            wtmp = tmp.with_suffix(".src.wav"); decode(path, wtmp)
            x = load_wav(wtmp).T.astype("float32")
            y = pedalboard.time_stretch(x, float(SR), stretch_factor=float(r), high_quality=True,
                                        transient_mode="crisp")
            save_wav(wtmp, np.clip(y.T, -1, 1).astype("float32"))
            src_in, src_af, af = str(wtmp), "anull", "rubberband (built in)"
        except Exception:
            log_exc("built-in time-stretch")
    res = subprocess.run([djgrab.FFMPEG, "-y", "-v", "error", "-i", src_in, "-map", "0:a:0", "-af", src_af,
                          "-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0", "-id3v2_version", "0",
                          str(tmp)], capture_output=True, text=True)
    Path(str(tmp.with_suffix(".src.wav"))).unlink(missing_ok=True)
    if (res.returncode or not tmp.exists()) and "soxr" in af:          # ffmpeg without soxr: its own resampler
        af = af.replace(":resampler=soxr:precision=28", "")
        res = subprocess.run([djgrab.FFMPEG, "-y", "-v", "error", "-i", str(path), "-map", "0:a:0", "-af", af,
                              "-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0", "-id3v2_version", "0",
                              str(tmp)], capture_output=True, text=True)
    if res.returncode or not tmp.exists():
        log(f"{path.name}: tempo fix failed: {res.stderr[-200:]}")
        tmp.unlink(missing_ok=True)
        return False
    old = ID3(str(path))
    new = ID3()
    for k, f in old.items():
        if k.startswith("GEOB"):
            if f.desc == "Serato Markers2":              # your cues and loops move with the music
                ents = []
                for name, pl in parse_markers2(f.data):
                    if name in ("CUE", "LOOP") and len(pl) >= 6:
                        pl = pl[:2] + struct.pack(">I", int(struct.unpack(">I", pl[2:6])[0] / r)) + pl[6:]
                        if name == "LOOP" and len(pl) >= 10:
                            pl = pl[:6] + struct.pack(">I", int(struct.unpack(">I", pl[6:10])[0] / r)) + pl[10:]
                    ents.append((name, pl))
                new.add(GEOB(encoding=0, mime=f.mime, desc=f.desc, data=markers2_bytes(ents)))
            continue                                     # grid, waveform, analysis: rebuilt for the new file
        if k.startswith(("TLEN", "TBPM", "PRIV")):
            continue
        new.add(f)
    new.setall("TBPM", [TBPM(encoding=3, text=f"{to_bpm:g}")])
    new.add(TXXX(encoding=3, desc=TEMPO_TAG, text=f"{from_bpm:.3f} to {to_bpm:g} BPM"))
    new.save(str(tmp), v2_version=3)
    if vol is not None:
        keep = Path(vol) / "_To Delete" / "DJ Grab originals before tempo fix" / path.name
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, keep)
    os.replace(tmp, path)
    log(f"{path.name}: tempo {from_bpm:.3f} -> {to_bpm:g} BPM ({af.split('=')[0]})")
    return True


# "Auto" intro length (bars = 0): DJ pools make 16-bar intros for dance music and 8 bars for hip hop, pop,
# R&B, Latin, rock and the rest
LONG_INTRO_TOPS = {"House", "Dance & Electronic", "Dance & House", "Techno", "EDM"}
LONG_INTRO_SUBS = {"Afro House", "Amapiano", "Tech House", "Melodic House", "Classic House", "Jersey Club",
                   "Baile Funk", "Dance Pop", "Dance", "Gqom"}


def auto_bars(path):
    parts = Path(path).parts
    top = parts[-3] if len(parts) >= 3 else ""
    sub = parts[-2] if len(parts) >= 2 else ""
    return 8          # every intro is 8 bars (32 beats); loop it in the DJ software when a longer one is needed


TIMING = LOG.parent / "timing.jsonl"


def save_timing(path, T, mins, bpm, result, make_intro):
    """One line per song: how long each step took, so the slow steps can be found and sped up."""
    try:
        note = str(result.get("note", ""))
        rec = {
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "song": Path(path).name,
            "folder": "/".join(Path(path).parts[-3:-1]),
            "song_min": round(float(mins), 2),
            "bpm": round(float(bpm), 2) if bpm else None,
            "total_s": round(sum(v for k, v in T.items() if not k.startswith("_")), 1),
            "steps_s": {k: round(v, 1) for k, v in T.items() if not k.startswith("_")},
            "intro_wanted": bool(make_intro),
            "intro_made": bool(result.get("intro")),
            "built_twice": "building another" in note or bool(T.get("_rebuilt")),
            "repaired": "repaired" in note,
            "flagged": note.startswith("Flagged"),
            "no_intro_reason": note if note.startswith(("Couldn't", "No intro", "Opens on")) else "",
            "devices": {"separation": DEVICES.get("demucs"), "beats": DEVICES.get("beats"),
                        "studio_stems": DEVICES.get("roformer")},
            "engine": ENGINE,
        }
        with open(TIMING, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        log_exc("timing record")


def process(path, bars, make_intro=True):
    from mutagen.id3 import ID3, TIT2, GEOB, COMM, TXXX
    path = Path(path)
    if not bars:
        bars = auto_bars(path)           # Auto: 16 bars for dance music, 8 for the rest
    id3 = ID3(str(path))
    grid = read_beatgrid(id3)             # Serato's BPM is used as a hint only; its grid position isn't trusted
    work = Path(tempfile.mkdtemp(prefix="djintro-"))
    try:
        use_usb_storage(path)
        global GENRE_TEMPOS, CUR_PATH
        CUR_PATH = str(path)
        cache_clean()
        GENRE_TEMPOS = genre_tempos(path)
        T = {"_t": time.time()}

        def mark(name):
            now = time.time(); T[name] = T.get(name, 0) + now - T["_t"]; T["_t"] = now
        emit("progress", stage="Reading audio", pct=8)
        wav = work / "in.wav"
        pre_wav = PREFETCH.pop(str(path), None)
        if pre_wav and Path(pre_wav).exists():
            shutil.move(pre_wav, wav)
        else:
            decode(path, wav)
        orig = load_wav(wav)
        mark("read")
        mins = len(orig) / SR / 60.0
        if no_intro(path):
            make_intro = False                      # you said no intro for this song
        want_intro = make_intro and not has_intro_version(path)
        intro_steps = ["intro build", "encode intro"] if want_intro else []
        cached = cache_get(audio_key(orig) + "-ana") is not None
        send_eta(["tempo check"] + ([] if cached else ["separation"]) + ["beat grid", "write cues"] + intro_steps, mins)

        def beats_for(audio):
            ck = cache_get(audio_key(audio) + "-beats")
            if ck is not None:
                return ck["beats"].astype(float), ck["downbeats"].astype(float)
            b, d = track_beats(audio)
            if b is not None:
                cache_put(audio_key(audio) + "-beats", beats=np.asarray(b, "float64"), downbeats=np.asarray(d, "float64"))
            return b, d
        tg = lambda k: str(id3[k].text[0]).strip() if k in id3 and id3[k].text else ""
        hint = None
        try:
            hint = djgrab.official_bpm(tg("TPE1"), tg("TIT2"))
        except Exception:
            log_exc("official BPM lookup")
        pre, stretched = None, False
        if not (grid and grid_locked(path)) and not id3.getall("TXXX:" + TEMPO_TAG) and RETEMPO:
            # a song a hair off a whole BPM (130.78) is stretched to exactly that BPM (131), so it mixes on round numbers
            emit("progress", stage="Checking the tempo", pct=10)
            b0, d0 = beats_for(orig)
            if b0 is not None:
                e0, l0 = onset_env(orig)
                c = constant_grid(b0, d0, e0 / (e0.max() + 1e-9) + l0 / (l0.max() + 1e-9), len(orig), hint)
                if c is not None:
                    target = float(round(c["bpm"]))
                    if 0.003 < abs(target - c["bpm"]) <= 0.25:
                        emit("progress", stage=f"Setting the tempo to exactly {target:g} BPM", pct=12)
                        if retempo(path, c["bpm"], target):
                            stretched = True
                            k = c["bpm"] / target
                            pre = (b0 * k, d0 * k)
                            id3 = ID3(str(path))
                            grid = None
                            decode(path, wav); orig = load_wav(wav)
                    else:
                        pre = (b0, d0)
        mark("tempo check")
        key = audio_key(orig)
        ana = cache_get(key + "-ana")
        send_eta(([] if ana is not None else ["separation"]) + ["beat grid", "write cues"] + intro_steps, mins)
        if ana is not None:                                   # analysed before: no stem separation needed
            emit("progress", stage="Using cached analysis", pct=60)
            lev = {k[4:]: ana[k].astype("float32") for k in ana if k.startswith("lev_")}
            stems = LiteStems(lev, lambda: separate(wav, work / "stems", orig))
            d_env, low = ana["denv"].astype("float32"), ana["low"].astype("float32")
        else:
            emit("progress", stage="Separating drums and vocals", pct=15)
            stems = separate(wav, work / "stems", orig)
            emit("progress", stage="Finding the beat", pct=65)
            d_env, low = onset_env(stems["drums"])
            cache_put(key + "-ana", denv=d_env, low=low, **{"lev_" + k: v for k, v in stem_levels(stems).items()})
        mark("separation" if ana is None else "cached analysis")
        send_eta(["beat grid", "write cues"] + intro_steps, mins)
        e_mix, l_mix = onset_env(orig)                   # the full mix too: some edits only show in it
        n_ = min(len(d_env), len(e_mix))
        sig = (d_env[:n_] / (d_env.max() + 1e-9) + low[:n_] / (low.max() + 1e-9)
               + e_mix[:n_] / (e_mix.max() + 1e-9))
        if grid and grid_locked(path):
            g = bars_from_constant(grid[0] - serato_offset(path), grid[1], len(orig))   # your locked grid
            write_grid, gsrc = False, "your locked grid"
        else:
            beats, downbeats = pre if pre is not None else beats_for(orig)
            gsrc = "beat tracker" + (", tempo set to a whole BPM" if stretched else "")
            if beats is not None:
                g = constant_grid(beats, downbeats, sig, len(orig), hint,
                                  alts=[e_mix / (e_mix.max() + 1e-9) + l_mix / (l_mix.max() + 1e-9)]) or fit_bars(beats, downbeats, low, len(orig), sig)
            else:
                gsrc = "backup tracker"                                       # backup: our own tracker
                first, bpm = find_grid(stems, grid[1] if grid else None, genre_of(path),
                                       alt=(grid[0] - serato_offset(path)) if grid else None)
                g = bars_from_constant(first, bpm, len(orig))
            write_grid = True
        bars_t, bpm = g["bars"], g["bpm"]
        if write_grid and sig is not None and len(bars_t) > 4:
            # (fix_half_beat is off: in bass house and ghetto house the bassline and open hats sit between the
            # kicks, and it moved good grids onto the off-beat - Bad Wolf, Let Me Bang. The beat tracker is right)
            bars_t, g = place_downbeats(bars_t, g, sig, orig, float(bpm), path.name)
            fb, moved = follow_band(bars_t, sig, bpm)
            if moved >= 0.006:                        # the band drifts: the grid follows it, marker every 8 bars
                bars_t = fb
                pbt_ = 60.0 / float(bpm)
                old = np.asarray(g["bars"], float)
                src_m = g["markers"] if len(g["markers"]) > 1 else \
                    [(float(old[j]), 32, None) for j in range(0, len(old) - 8, 8)] + [(float(old[(len(old) - 1) // 8 * 8]), None, float(bpm))]
                mk_ = [(float(m[0] + np.interp(m[0], old, fb - old)), m[1], m[2]) for m in src_m]
                mk_ = [(mk_[i][0], int(round((mk_[i + 1][0] - mk_[i][0]) / pbt_)), None) for i in range(len(mk_) - 1)] + [mk_[-1]]
                g = dict(g, bars=fb, markers=mk_)
                log(f"{path.name}: grid follows the band (moved up to {moved * 1000:.0f} ms)")
        bar = float(np.median(np.diff(bars_t)))
        emit("progress", stage="Finding the sections", pct=72)
        official = None
        try:
            tags = lambda k: str(id3[k].text[0]).strip() if k in id3 and id3[k].text else ""
            official = djgrab.lookup_duration(tags("TPE1"), tags("TIT2"))
        except Exception:
            log_exc("official length lookup")
        cmt = ""
        try:                                              # Serato's Comment column: era + moment tags
            tg2 = lambda k: str(id3[k].text[0]).strip() if k in id3 and id3[k].text else ""
            cmt = djgrab.song_comment(tg2("TPE1") or path.stem.partition(" - ")[0],
                                      tg2("TIT2") or path.stem.partition(" - ")[2], tg2("TDRC"))
        except Exception:
            log_exc("song comment")
        mark("beat grid")
        times, info = analyse_bars(stems, bars_t, len(orig), official)
        info["drift"] = float(g["drift"])
        # cues every 8 bars on the grid from the song's first note (the first bar with sound)
        sb0 = int(info["sb"])
        first_note = first_sound(orig, float(bars_t[min(sb0 + 1, len(bars_t) - 1)]) + 0.1)
        if first_note is None:
            first_note = float(bars_t[sb0])
        if float(bars_t[sb0]) - first_note > 4 * 60.0 / float(bpm):  # more than a bar early: noise, not a pickup
            first_note = float(bars_t[sb0])
        if os.environ.get("DJ_DEBUG"):
            print("DEBUG cue", "bars", [round(float(v), 3) for v in bars_t[:4]], "sb0", sb0, "first_note", first_note)
        head_ = [("Start", first_note * 1000, 0x00CC00)]
        hv = intro_halfway(path, bars_t, sb0, times)
        if hv is not None:                       # an intro version (ours or a pool's): Halfway through its intro,
            head_.append(("Halfway", hv * 1000, 0x00CCCC))     # then Song where the song itself comes in
            vb_ = int(np.argmin(np.abs(np.asarray(bars_t) - times["Vocals"])))
            head_.append(("Song", float(bars_t[vb_]) * 1000, 0x0088FF))
        cues = pads(head_ +
                    phrase_cues(bars_t, sb0, times, [c for c in OUR_CUES if c[0] != "Start"],
                                [("Start", float(bars_t[sb0]) * 1000, 0)] + head_[1:]))
        written = write_cues(str(path), cues, g["markers"] if write_grid else None)
        _other_app(path, cues, bpm, float(bars_t[0]))
        mark("write cues")
        if stretched:
            mark_analysed(path, id3, orig, float(bpm))
            watch_grid(path)
        result = {"status": "done", "cues": written, "bpm": round(float(bpm), 1)}
        sig_parts = [f"{float(bpm):.2f}", str(int(round(float(bars_t[0]) * 100)))] + \
                    [f"{n}:{int(round(v * 20))}" for n, v in sorted(times.items()) if isinstance(v, float) and not n.startswith("_")]
        _top, _sub = path.parent.parent.name, path.parent.name
        need = needs_intro(times, bar, dance=_top in LONG_INTRO_TOPS or _sub in LONG_INTRO_SUBS) or force_intro(path)
        # a short spoken or a cappella opening (up to 4 bars, no beat) and then 8 clean bars of the full beat
        # with no vocals (Timbaland's Give It To Me): the song already has its own DJ intro, so a made one
        # would just be a second intro in front of it
        own_intro = None
        if need and not force_intro(path):
            sbi, v_, d_, nb_i = int(info["sb"]), info["voc"], info["drm"], int(info["nb"])
            dref_ = float(np.median(d_[d_ > 0])) if np.any(d_ > 0) else 0.0
            for j in range(sbi + 1, min(sbi + 5, nb_i - 8)):
                if (float(d_[sbi:j].max()) < 0.2 * dref_ and float(d_[j:j + 8].min()) >= 0.4 * dref_
                        and float(v_[j:j + 8].mean()) < 0.25 * info["v90"] and float(v_[j:j + 8].max()) < 0.6 * info["v90"]):
                    own_intro = j
                    break
            if own_intro is not None:
                need = False
        if make_intro and not need:
            early = times.get("Vocals") is not None and times["Vocals"] - times["Start"] < 7.75 * bar
            result["note"] = "Has a clean 8-bar intro" + (" (only ad-libs over the beat)" if early else "")
            if own_intro is not None:
                result["note"] = f"Has its own 8-bar beat intro after a {own_intro - int(info['sb'])}-bar spoken opening"
            if intro_path(path).exists() and is_our_intro(intro_path(path)):
                retire_intro(intro_path(path))
                result["note"] += " · old intro moved to _To Delete"
        elif make_intro and not has_intro_version(path):
            emit("progress", stage=f"Building the {bars}-bar intro", pct=80)
            mark("cues")
            send_eta(["intro build", "encode intro"], mins)
            info["vox_share"] = float(times.get("_vox_share", 0.0))
            info["top"] = path.parent.parent.name
            info["bpm"] = float(bpm)
            info["dance"] = path.parent.parent.name in LONG_INTRO_TOPS or path.parent.name in LONG_INTRO_SUBS
            if isinstance(times.get("Drop"), float):           # the chorus, for the hook tease
                info["chorus_bar"] = int(np.argmin(np.abs(np.asarray(bars_t) - times["Drop"])))
            info["layer"] = path.parent.parent.name in LAYER_GENRES or path.parent.name in LAYER_GENRES
            global CLAP_NEXT
            # the clap build: songs with no real beat up front and little drum anywhere, or "Try another"
            # after the loop versions were turned down
            drm_ = info.get("drm")
            light = drm_ is not None and float(np.percentile(drm_, 90)) < 0.02
            use_claps = False          # clap builds are off: they didn't sound good (kept in the code, never used)
            CLAP_NEXT = False
            try:
                if use_claps:
                    CLAP_NEXT = False
                    global REDO_PICK
                    REDO_PICK = "0:claps"
                    audio, ibars, style, half_cue, offset = build_clap_intro(orig, stems, bars_t, info, bars)
                else:
                    audio, ibars, style, half_cue, offset = build_intro_bars(orig, stems, bars_t, info, bars)
            except OpensOnHook:
                audio = None
                result["note"] = "Opens on its hook: no intro (cut it in on beat 1)"
                if intro_path(path).exists() and is_our_intro(intro_path(path)):
                    retire_intro(intro_path(path))
                    result["note"] += " · old intro moved to _To Delete"
            if audio is not None:
                mark("intro build")
                send_eta(["encode intro"], mins)
                s0_ = info.get("song_from", info["sb"])
                orig_song = orig[int(round(float(bars_t[s0_]) * SR)):]
                flags = check_intro(audio, ibars, offset, float(bpm), orig_song)
                try:                                  # fix jumps and missing hits first, then check again
                    fixed_a, fixed_what, mv = repair_intro(audio, ibars, offset, float(bpm))
                    if fixed_what:
                        ff = check_intro(fixed_a, ibars, offset, float(bpm), _song_moved(orig_song, mv))
                        if not any(f in SERIOUS for f in ff):        # a repair is only used when it comes out clean
                            log(f"{path.name}: intro repaired ({'; '.join(fixed_what)})" + (f", check now: {', '.join(ff)}" if ff else ""))
                            audio, flags = fixed_a, ff
                            style += " · repaired: " + "; ".join(fixed_what)
                except Exception:
                    log_exc("intro repair")
                flagged = None
                if any(f in SERIOUS for f in flags) and not use_claps:
                    # built again another way (the next-best version from another stretch of the song), up to
                    # 3 versions in all; the version with the fewest problems is kept
                    log(f"{path.name}: intro check found {', '.join(flags)}, building another")
                    T["_rebuilt"] = 1
                    picks = [REDO_PICK]
                    for attempt in (2, 3):
                        if not any(f in SERIOUS for f in flags):
                            break
                        try:
                            for p_ in picks:
                                REDO_TRIED.add(p_ or "")
                            avoid = {int(str(p_).split(':')[0]) for p_ in picks if p_ and str(p_).split(':')[0].isdigit()}
                            a2 = build_intro_bars(orig, stems, bars_t, dict(info, avoid=avoid), bars)
                            picks.append(REDO_PICK)
                            f2 = check_intro(a2[0], a2[1], a2[4], float(bpm), orig_song)
                            try:
                                ra, rw, mv2 = repair_intro(a2[0], a2[1], a2[4], float(bpm))
                                if rw:
                                    rf = check_intro(ra, a2[1], a2[4], float(bpm), _song_moved(orig_song, mv2))
                                    if not any(f in SERIOUS for f in rf):        # a repair is only used when it comes out clean
                                        a2 = (ra, a2[1], a2[2] + " · repaired: " + "; ".join(rw), a2[3], a2[4]); f2 = rf
                            except Exception:
                                log_exc("intro repair")
                            log(f"{path.name}: version {attempt} check: {', '.join(f2) or 'clean'}")
                            if sum(f in SERIOUS for f in f2) < sum(f in SERIOUS for f in flags):
                                audio, ibars, style, half_cue, offset = a2
                                flags = f2
                        except Exception:
                            log_exc(f"intro version {attempt}")
                        finally:
                            for p_ in picks:
                                REDO_TRIED.discard(p_ or "")
                if any(f in SERIOUS for f in flags):
                    # no version came out clean: the best one is still made, marked in the file (and listed in
                    # logs/flagged_intros.jsonl) so the problem songs can be gathered and what they share fixed
                    flagged = [f for f in flags if f in SERIOUS]
                    log(f"{path.name}: intro kept but flagged ({', '.join(flags)})")
                    style += " · FLAGGED: " + ", ".join(flagged)
                if audio is not None:
                    tmp = work / "intro.wav"; save_wav(tmp, audio)
                    # built and tagged on the Mac's fast disk, then copied to the USB once (not rewritten there 3 times)
                    dst = intro_path(path); part = work / "intro.part.mp3"
                    r = subprocess.run([djgrab.FFMPEG, "-y", "-v", "error", "-i", str(tmp), "-c:a", "libmp3lame", "-b:a", "320k",
                                        "-compression_level", "2", "-id3v2_version", "0", str(part)], capture_output=True, text=True)
                    if r.returncode:
                        raise RuntimeError("couldn't encode the intro: " + r.stderr[-200:])
                    new = ID3()
                    for k, f in id3.items():
                        if not k.startswith(("GEOB", "PRIV", "TLEN", "PCNT", "POPM")) and "PLAYCOUNT" not in k.upper() and k != "TXXX:DJ Grab trim":
                            new.add(f)
                    title = str(id3.get("TIT2", path.stem.partition(" - ")[2] or path.stem))
                    art = str(id3.get("TPE1", "")).strip()
                    if art and title.lower().startswith(art.lower() + " - "):
                        title = title[len(art) + 3:]               # a title that repeats the artist ("BTS - Dynamite")
                    new.setall("TIT2", [TIT2(encoding=3, text=f"{title} (Intro)")])
                    new.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=cmt or "")])
                    new.setall("TXXX:DJ Grab intro", [TXXX(encoding=3, desc="DJ Grab intro", text=f"{bars} bars, {style}")])
                    if flagged:
                        new.setall("TXXX:DJ Grab check", [TXXX(encoding=3, desc="DJ Grab check", text="FLAGGED: " + ", ".join(flags))])
                        try:
                            with open(LOG.parent / "flagged_intros.jsonl", "a") as f_:
                                f_.write(json.dumps({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "song": path.name,
                                                     "folder": "/".join(path.parts[-3:-1]), "flags": flags, "bpm": float(bpm),
                                                     "drift": float(g.get("drift", 0) or 0), "style": style,
                                                     "versions_tried": 3}) + "\n")
                        except Exception:
                            log_exc("flag record")
                    new.save(str(part), v2_version=3)
                    # the intro file's bar lines: the intro's own bars, then the song's bars shifted to follow it
                    sb = info.get("song_from", info["sb"])        # where the song comes in (after any skipped quiet bars)
                    shift = offset - float(bars_t[sb])
                    all_bars = np.concatenate([ibars, bars_t[sb:] + shift])   # the song's pickup bars count in the intro
                    if g["drift"] <= 0.012 and len(g.get("markers") or []) <= 1:
                        imarkers = [(float(all_bars[0]), None, float(bpm))]
                    else:
                        imarkers = [(float(all_bars[j]), 8, None) for j in range(0, len(all_bars) - 2, 2)]
                        imarkers.append((float(all_bars[len(imarkers) * 2]), None, float(bpm)))
                    # the intro's first bar, halfway to the song, the song's start, then every 8 bars of the song
                    head = [("Start", float(all_bars[0]) * 1000, 0x00CC00), ("Halfway", float(all_bars[bars // 2]) * 1000, 0x00CCCC),
                            ("Song", float(all_bars[min(bars, len(all_bars) - 1)]) * 1000, 0x0088FF)]   # where the song comes in
                    st_ = {n: v + shift for n, v in times.items() if isinstance(v, float) and v > float(bars_t[sb]) + 0.5 * bar}
                    ph0 = bars                                     # 8-bar cycle counted from Song Start
                    rest = phrase_cues(all_bars, min(ph0, len(all_bars) - 1), st_, [c for c in OUR_CUES if c[0] != "Start"], head)
                    icues = pads(head + rest)
                    mark("encode intro")
                    result["intro_cues"] = write_cues(str(part), icues, imarkers)
                    _other_app(dst, icues, bpm, 0.0)
                    mark_analysed(part, id3, audio, bpm)
                    usb_tmp = dst.with_name("." + dst.stem + ".part.mp3")
                    shutil.copyfile(part, usb_tmp)
                    os.replace(usb_tmp, dst)
                    mark("intro tags")
                    watch_grid(dst)
                    djgrab.queue_for_crate(dst)
                    result["intro"] = str(dst)
                    result["note"] = (("Flagged for review (" + ", ".join(flagged) + ") · ") if flagged else "") + \
                        "Intro: " + style + (" · check: " + ", ".join(flags) if flags else "")
        elif make_intro:
            result["note"] = "Has an intro version"
        if make_intro and MAKE_OUTRO and not outro_path(path).exists():
            try:
                made = build_outro(orig, stems, bars_t, info)
                if made is not None:
                    oaudio, ostart, obars = made
                    work_o = Path(tempfile.mkdtemp(prefix="djout-"))
                    tmp = work_o / "outro.wav"; save_wav(tmp, oaudio)
                    part = work_o / "outro.part.mp3"
                    r = subprocess.run([djgrab.FFMPEG, "-y", "-v", "error", "-i", str(tmp), "-c:a", "libmp3lame", "-b:a", "320k",
                                        "-compression_level", "2", "-id3v2_version", "0", str(part)], capture_output=True, text=True)
                    if r.returncode:
                        raise RuntimeError("couldn't encode the outro: " + r.stderr[-200:])
                    new = ID3()
                    for k, f in id3.items():
                        if not k.startswith(("GEOB", "PRIV", "TLEN", "PCNT", "POPM")) and "PLAYCOUNT" not in k.upper() and k != "TXXX:DJ Grab trim":
                            new.add(f)
                    title = str(id3.get("TIT2", path.stem.partition(" - ")[2] or path.stem))
                    new.setall("TIT2", [TIT2(encoding=3, text=f"{title} (Outro)")])
                    new.setall("TXXX:DJ Grab outro", [TXXX(encoding=3, desc="DJ Grab outro", text="16 bars of beat after the vocals")])
                    new.save(str(part), v2_version=3)
                    # the song's own cues up to where the outro beat starts, then Outro there
                    ocues = [c for c in cues if c[1] != "Outro" and c[2] < ostart * 1000 - 100]
                    ocues = pads([(n, ms, rgb) for _, n, ms, rgb in ocues] + [("Outro", ostart * 1000, 0x8800CC)])
                    steady = g["drift"] <= 0.012 and len(g.get("markers") or []) <= 1
                    write_cues(str(part), ocues, (float(bars_t[0]), float(bpm)) if steady else None)
                    dst = outro_path(path)
                    tmp_usb = dst.with_name("." + dst.stem + ".part.mp3")
                    shutil.copyfile(part, tmp_usb)
                    os.replace(tmp_usb, dst)
                    shutil.rmtree(work_o, ignore_errors=True)
                    _other_app(dst, ocues, bpm, float(bars_t[0]))
                    djgrab.queue_for_crate(dst)
                    result["outro"] = str(dst)
                    result["note"] = (result.get("note", "") + " · " if result.get("note") else "") + "Outro version made"
            except Exception:
                log_exc("outro")
        mark("finish")
        learn_steps(T, mins)
        save_index()
        log("timing: " + ", ".join(f"{k} {v:.1f}s" for k, v in T.items() if not k.startswith("_")) +
            f" | devices: separation {DEVICES.get('demucs', '?')}, beats {DEVICES.get('beats', '?')}, "
            f"studio stems {DEVICES.get('roformer', '-')}")
        save_timing(path, T, mins, bpm, result, make_intro)
        try:
            tr = set_trim(path, orig, bpm)
            log(f"{path.name}: " + (f"trim +{tr:.1f} dB (loudest peak to -{TRIM_HEADROOM} dB)" if tr is not None
                                    else "trim left as you set it"))
            if intro_path(path).exists() and is_our_intro(intro_path(path)):
                set_trim(intro_path(path), orig, bpm)     # same peak level as the song it's built on
        except Exception:
            log_exc("trim")
        try:
            t3 = ID3(str(path))
            if djgrab.set_comment(t3, cmt):
                t3.save(str(path), v2_version=3)
            ip = intro_path(path)                         # our intro version carries the same comment
            if ip.exists() and is_our_intro(ip):
                t4 = ID3(str(ip))
                legacy = [str(c) for c in t4.getall("COMM") if str(c).startswith("DJ Grab intro")]
                if legacy and not t4.getall("TXXX:DJ Grab intro"):
                    t4.add(TXXX(encoding=3, desc="DJ Grab intro", text=legacy[0].replace("DJ Grab intro: ", "")))
                if legacy:
                    t4.delall("COMM")
                if djgrab.set_comment(t4, cmt) or legacy:
                    t4.save(str(ip), v2_version=3)
        except Exception:
            log_exc("write comment")
        # (an a cappella start/end is only noted in the log: originals keep their real names, you asked; a label
        # belongs on a version DJ Grab actually changed)
        genre_profile_update(path, bpm, str(result.get("note", "")))
        result["sig"] = "|".join(sig_parts + [re.split(r" · (?:check|will be named)", str(result.get("note", "")))[0]])
        log(f"{path.name}: {float(bpm):.2f} BPM ({gsrc}, drift {g['drift'] * 1000:.0f} ms), first bar {info['sb']}, "
            f"phrases from bar {info.get('anchor')}, file {len(orig) / SR:.0f} s vs official {official or 0:.0f} s, "
            f"skipped {info.get('talk', 0)} bars before the song, voice up front {100 * times.get('_vox_share', 0):.0f}% of the "
            f"opening (longest run {100 * times.get('_vox_run', 0):.0f}%), comment '{cmt}', a cappella "
            f"{acap_label(times) or 'no'}, cues {written} — {result.get('note', '')}")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ unplug-safe waiting
def usb_ok(path):
    root = djgrab.volume_root(path)
    return root is None or os.path.ismount(root)


def wait_ready(path):
    """Wait (without failing) for the USB to be plugged in and Serato to be closed."""
    told = None
    while True:
        if not usb_ok(path):
            if told != "usb":
                emit("paused", message="USB unplugged. Paused; it picks up where it left off when you plug it back in."); told = "usb"
            time.sleep(5); continue
        if djgrab.serato_running():
            if told != "serato":
                emit("paused", message="Paused while Serato is open. It continues when you close Serato."); told = "serato"
            time.sleep(15); continue
        if told:
            emit("resumed", message="Back to work.")
        return


def another_intro(target, bars):
    """'Try another intro': the current intro version goes to _To Delete (never deleted) and a new one is made
    from a version of the song that wasn't heard yet: another loop of the same stretch, then another stretch."""
    global REDO_TRIED, REDO_PICK
    name = Path(target).stem
    if not os.path.exists(target):
        emit("error", name=name, path=target, message="File not found (moved or deleted)")
        return
    try:
        tries = json.loads(TRIED_FILE.read_text())
    except Exception:
        tries = {}
    key = unicodedata.normalize("NFC", target)
    REDO_TRIED = set(tries.get(key, []))
    global CLAP_NEXT
    CLAP_NEXT = len([k for k in REDO_TRIED if not k.endswith(":claps")]) >= 3 and "0:claps" not in REDO_TRIED
    global REDO_SKIP_BEST
    REDO_SKIP_BEST = not REDO_TRIED
    wait_ready(target)
    emit("item_start", name=name, path=target, index=1, total=1)
    ip = intro_path(Path(target))
    if ip.exists():
        retire_intro(ip)
    REDO_PICK = None
    r = _run_quiet(target, bars)
    if REDO_PICK:
        order = list(tries.get(key, [])) + [k for k in REDO_TRIED if k not in tries.get(key, [])]
        tries[key] = [k for k in order if k != REDO_PICK] + [REDO_PICK]     # last = the intro there now
        TRIED_FILE.write_text(json.dumps(tries))
    REDO_TRIED = set()
    REDO_SKIP_BEST = False
    if r.get("status") == "done" and "intro" not in r:
        r["note"] = "Couldn't make a different intro that fits. " + (r.get("note") or "")
    emit(r.pop("status"), name=name, path=target, **r)


def _run_quiet(p, bars):
    """run_one without it recording its pick as the first one (another_intro keeps the full list)"""
    return run_one(p, bars)


def run_one(p, bars):
    """Process a song; if the USB disappears mid-song, wait for it and redo that song."""
    while True:
        wait_ready(p)
        t0 = time.time()
        try:
            r = process(p, bars, MAKE_INTRO)
        except Exception as ex:
            if not usb_ok(p):
                continue                  # unplugged mid-song: not counted, retried after reconnect
            log_exc(f"{Path(p).name}")
            r = {"status": "error", "message": str(ex)[:300]}
        if not usb_ok(p):
            continue
        r["secs"] = round(time.time() - t0, 1)
        if REDO_PICK and not REDO_TRIED and not REDO_SKIP_BEST and r.get("intro"):
            try:
                tries = json.loads(TRIED_FILE.read_text())
            except Exception:
                tries = {}
            tries[unicodedata.normalize("NFC", str(p))] = [REDO_PICK]
            try:
                TRIED_FILE.write_text(json.dumps(tries))
            except Exception:
                pass
        return r


# ------------------------------------------------------------------ batch
def eligible(root):
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root)
        dn[:] = sorted(d for d in dn if not d.startswith(("_", ".")) and not (rel == "." and d in SKIP_FOLDERS))
        for f in sorted(fn):
            if f.startswith(".") or not f.lower().endswith(".mp3") or ".part." in f or f.endswith(".djgrab-tmp"):
                continue
            if SKIP_WORDS.search(f):
                continue
            yield os.path.join(dp, f)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"done": {}}


def save_state(st):
    djgrab.APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    STATE.with_suffix(".tmp").write_text(json.dumps(st)); STATE.with_suffix(".tmp").replace(STATE)


def settled(st, k):
    """A song that came out "no intro needed" is final: later redos and engine updates skip it (faster runs).
    Older records with no note count too when they finished fine and have no intro file beside them."""
    if k in st.get("done", {}) and st.get("grid", {}).get(k) != GRID_RULE:
        return False                # beat-grid finding improved since: one quick recheck (cached analysis)
    if st.get("stable", {}).get(k):
        # ...unless it got an intro under an older "does it need one" rule: then it's looked at once more
        # (from the cache, quick); a song that no longer needs one has its intro moved to _To Delete
        # (came out the same on two runs in a row: solid, not redone)
        return not (st.get("kind", {}).get(k) == "intro" and st.get("rule", {}).get(k) != INTRO_RULE)
    kind = st.get("kind", {}).get(k)
    if kind is not None:
        return kind == "clean"
    v = st["done"].get(k) or ""
    return v.startswith("done@") and not intro_path(Path(k)).exists()


def ran(st, k):
    """This song has been through DJ Grab (any version of it) and didn't fail or get put off."""
    v = st.get("done", {}).get(k) or ""
    return bool(v) and not v.startswith(("failed", "error", "later"))


def folder_status(root):
    """Songs done vs total for every genre folder and subfolder, so the app can tick finished ones.
    A folder in the middle of a redo only counts songs redone since that redo started."""
    st = load_state()
    when, redo = st.get("when", {}), st.get("redo", {})
    done_keys = {unicodedata.normalize("NFC", k) for k, v in st["done"].items()
                 if v and v.endswith(ENGINE) and not needs_recheck(st, k)}      # an update to check = not done yet
    out = {}
    for top in sorted(os.listdir(root)):
        tp = os.path.join(root, top)
        if top.startswith((".", "_")) or top in SKIP_FOLDERS or not os.path.isdir(tp):
            continue
        for p in eligible(tp):
            rel = os.path.relpath(p, root).split(os.sep)
            k = unicodedata.normalize("NFC", p)
            keys = [rel[0]] + ([rel[0] + "/" + rel[1]] if len(rel) > 2 else [])
            for key in keys:
                since = redo.get(unicodedata.normalize("NFC", os.path.join(root, key))) or redo.get(unicodedata.normalize("NFC", root))
                # ticked once every song in it has been run, and it stays ticked until something in the
                # folder changes (a new or renamed song); DJ Grab updates don't untick it
                hit = ran(st, k) and (since is None or when.get(k, 0) >= since or settled(st, k))
                d = out.setdefault(unicodedata.normalize("NFC", key), [0, 0])
                d[1] += 1
                d[0] += hit
    # the chosen folder as a whole (any folder on the computer, songs in it and in its subfolders): "."
    d = out.setdefault(".", [0, 0])
    since = redo.get(unicodedata.normalize("NFC", root))
    for p in eligible(root):
        k = unicodedata.normalize("NFC", p)
        d[1] += 1
        d[0] += ran(st, k) and (since is None or when.get(k, 0) >= since or settled(st, k))
    emit("status", folders=out)


def batch(root, bars, redo=False):
    """Work through a folder. A plain run skips songs already done (so a paused run picks up where it
    left off). A redo run (folder already finished, Start pressed again) does every song again and
    overwrites the old cues and DJ Grab intros; if it's paused, the next Start carries on the redo."""
    st = load_state()
    nk = unicodedata.normalize("NFC", root)
    st.setdefault("when", {}); st.setdefault("redo", {})
    if redo and nk not in st["redo"]:
        st["redo"][nk] = time.time(); save_state(st)
    since = st["redo"].get(nk)
    every = list(eligible(root))

    def is_done(p):
        k = unicodedata.normalize("NFC", p)
        if since is None:
            # a plain run picks up what changed: songs new to the folder (or renamed), ones that failed or
            # were put off, and finished songs an update could change (see RULE_CHANGES). Everything else
            # already done is skipped in a quick scan
            return ran(st, k) and not (MAKE_INTRO and needs_recheck(st, k))
        if settled(st, k):
            return True
        if not (st["done"].get(k) or "").endswith(ENGINE):
            return False
        return st["when"].get(k, 0) >= since

    # intro versions (yours from DJ pools and older ones of ours) get Start and Halfway cues, once
    if not PLAIN:
        ih = st.setdefault("ihalf", {})
        extra = [p for p in intro_files_missing_halfway(root) if unicodedata.normalize("NFC", p) not in ih]
        for i, p in enumerate(extra, 1):
            if not os.path.exists(p):
                continue
            emit("item_start", name=Path(p).stem, path=p, index=i, total=len(extra))
            try:
                r = process(p, bars, False)
            except Exception as ex:
                log_exc(f"intro cues {Path(p).name}")
                r = {"status": "error", "message": str(ex)[:300]}
            ih[unicodedata.normalize("NFC", p)] = 1
            save_state(st)
            emit(r.get("status", "done"), name=Path(p).stem, path=p, **{k: v for k, v in r.items() if k not in ("status", "sig", "path")})
    todo = [p for p in every if not is_done(p)]
    total = len(every)
    emit("batch", total=total, done=total - len(todo), left=len(todo), redo=since is not None)
    ensure_demucs()
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)

    def _prefetch(q):
        try:
            out = Path(tempfile.mkdtemp(prefix="djpre-")) / "in.wav"
            decode(q, out)
            PREFETCH[str(Path(q))] = str(out)
        except Exception:
            pass
    for i, p in enumerate(todo, 1):
        wait_ready(p)
        if not os.path.exists(p):
            continue                       # moved or deleted since the list was made
        if i < len(todo):
            pool.submit(_prefetch, todo[i])            # read + decode the next song off the USB meanwhile
        emit("item_start", name=Path(p).stem, path=p, index=total - len(todo) + i, total=total)
        r = run_one(p, bars)
        if r["status"] != "later":
            k = unicodedata.normalize("NFC", p)
            st["done"][k] = r["status"] + ENGINE; st["when"][k] = time.time()
            if r["status"] == "done" and MAKE_INTRO:
                st.setdefault("rule", {})[k] = INTRO_RULE
                st.setdefault("grid", {})[k] = GRID_RULE
                st.setdefault("kind", {})[k] = "clean" if "intro" not in r and str(r.get("note", "")).startswith(("Has a clean", "Opens on its hook")) else "intro"
            if r["status"] == "done" and r.get("sig") and "check:" not in str(r.get("note", "")):
                prev = st.setdefault("sig", {}).get(k)
                st["sig"][k] = r["sig"]
                if prev == r["sig"]:
                    st.setdefault("stable", {})[k] = True
            save_state(st)
        emit(r["status"], name=Path(p).stem, path=p, **{k: v for k, v in r.items() if k not in ("status", "sig", "path")})
    pool.shutdown(wait=False)
    try:
        n = 0 if PLAIN else djgrab.plan_cleanup(root)     # others' copies never rename their files
        if n:
            emit("info", message=f"Name cleanup: {n} songs queued (done when Serato is closed)")
            log(f"name cleanup queued for {n} songs in {root}")
    except Exception:
        log_exc("name cleanup")
    save_index()
    for w in list(PREFETCH.values()):
        shutil.rmtree(Path(w).parent, ignore_errors=True)
    PREFETCH.clear()
    st["redo"].pop(nk, None); save_state(st)
    djgrab.flush_crates()
    emit("finished", message="All done")


def plain_intro(target, bars):
    """The shared app's intro maker (works with any DJ software): the song is worked on as a temporary copy,
    so nothing is written into it (no cue points, grids, trim or comments). Only the intro version is added,
    next to the song, with ordinary tags; nothing of Serato's or any other program's goes in it."""
    import shutil, tempfile
    from mutagen.id3 import ID3
    src = Path(target)
    name = src.stem
    if not src.exists():
        emit("skipped", name=name, path=target, message="File not found (moved or deleted)")
        return
    emit("item_start", name=name, path=target, index=1, total=1)
    work = Path(tempfile.mkdtemp(prefix="djgrab-intro-"))
    try:
        cp = work / src.name
        shutil.copy2(src, cp)
        djgrab.queue_for_crate = lambda *a, **k: None       # no crates, no DJ software of any kind
        r = process(str(cp), bars, True)
        ip = intro_path(cp)
        if ip.exists():
            out = intro_path(src)
            serato = os.environ.get("DJGRAB_DJAPP", "serato").lower() == "serato"
            try:
                t = ID3(str(ip))
                for k in list(t.keys()):
                    # Serato users keep the intro's cue points and grid (they live in the file); for any
                    # other DJ software nothing of Serato's goes in it (their cues go to that app's library file)
                    if k.startswith(("PRIV", "TXXX:DJ Grab", "TXXX:SERATO")) or (k.startswith("GEOB") and not serato):
                        t.delall(k)
                t.save(str(ip), v2_version=3)
            except Exception:
                pass
            shutil.move(str(ip), str(out))
            try:                                   # the cue list for Rekordbox / Traktor / VirtualDJ follows the file
                import djcues
                d_ = djcues._load()
                k_ = str(Path(ip).resolve())
                if k_ in d_:
                    d_[str(out.resolve())] = d_.pop(k_)
                    djcues._save(d_)
                    djcues.export(out.parent, d_)
            except Exception:
                log_exc("cue list")
            emit("done", name=name, path=target, intro=str(out), note=r.get("note"))
        else:
            emit("done", name=name, path=target, note=r.get("note") or "No intro needed")
    except Exception as ex:
        emit("error", name=name, path=target, message=f"{type(ex).__name__}: {ex}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    global JSON
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["batch", "one", "files", "status", "intro", "another"])
    ap.add_argument("target", nargs="*")
    ap.add_argument("--root", default="/Volumes/Vinny's USB/Vinny's USB")
    ap.add_argument("--bars", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-intro", action="store_true")
    ap.add_argument("--redo", action="store_true", help="do every song again (overwrites old cues and intros)")
    a = ap.parse_args()
    JSON = a.json
    djgrab.JSON_MODE = a.json
    djgrab.FFMPEG, djgrab.FFPROBE = djgrab.find_tool("ffmpeg"), djgrab.find_tool("ffprobe")
    global MAKE_INTRO
    MAKE_INTRO = not a.no_intro
    if a.mode == "status":
        folder_status(a.root)
    elif a.mode == "intro":
        ensure_demucs()
        for target in a.target:
            plain_intro(target, a.bars)
    elif a.mode == "batch":
        if not PLAIN and not CUES_V2.exists() and os.path.isdir(a.root):
            wait_ready(a.root)
            lib = next((q for q in [Path(a.root)] + list(Path(a.root).parents)
                        if (q / "Hip Hop").is_dir() and (q / "Pop").is_dir()), Path(a.root))
            n = tidy_cues(lib)
            CUES_V2.write_text(str(n))
            emit("info", message=f"Cue update: {n} songs now use Start, Chorus, Breakdown, Outro (and Halfway on intros)")
        batch(a.root, a.bars, a.redo)
    elif a.mode == "another":
        ensure_demucs()
        for target in a.target:
            another_intro(target, a.bars)
        djgrab.flush_crates()
    else:
        ensure_demucs()
        for target in a.target:
            wait_ready(target)
            if not os.path.exists(target):
                emit("skipped", name=Path(target).stem, path=target, message="File not found (moved or deleted)")
                continue
            wait_ready(target)
            emit("item_start", name=Path(target).stem, path=target, index=1, total=len(a.target))
            r = run_one(target, a.bars)
            if r["status"] != "later":
                st = load_state(); k = unicodedata.normalize("NFC", target)
                st["done"][k] = r["status"] + ENGINE; st.setdefault("when", {})[k] = time.time(); save_state(st)
            emit(r.pop("status"), name=Path(target).stem, path=target, **r)
        djgrab.flush_crates()


if __name__ == "__main__":
    main()
