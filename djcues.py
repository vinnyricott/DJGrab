"""Cue points for DJ software other than Serato.

Serato reads cues from the file's own tags (djtools.write_cues). Rekordbox, Traktor and VirtualDJ keep
cues in their own library, so DJ Grab keeps a list of every track it has cued (cue_list.json) and, after
each track, rewrites a library file the chosen app can read:

  rekordbox  "DJ Grab cues/DJ Grab (rekordbox).xml"   Preferences > Advanced > Database > rekordbox xml
  traktor    "DJ Grab cues/DJ Grab (Traktor).nml"     File > Import Collection (or drag onto Collection)
  virtualdj  written into VirtualDJ's own database.xml while VirtualDJ is closed; otherwise queued and
             written the next time VirtualDJ is closed

The chosen app comes from DJGRAB_DJAPP (set by the app from the first-launch setup).
Times are the decoder's (the same as the audio you hear), in seconds from the start of the file.
"""
import json, os, platform, shutil, subprocess, time
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

import djgrab

LIST = djgrab.APP_SUPPORT / "cue_list.json"
APP = os.environ.get("DJGRAB_DJAPP", "serato").lower()
WIN = platform.system() == "Windows"


def _load():
    try:
        return json.loads(LIST.read_text())
    except Exception:
        return {}


def _save(d):
    tmp = LIST.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, LIST)


def remember(path, cues, bpm, first_beat, title="", artist=""):
    """cues: (slot, name, ms, rgb) in decoder time. Called after every cue write, whatever the app."""
    if APP in ("serato", "other", ""):
        return
    d = _load()
    d[str(Path(path).resolve())] = {
        "cues": [[int(s), n, float(ms) / 1000.0, int(rgb)] for s, n, ms, rgb in cues],
        "bpm": float(bpm or 0), "first": float(first_beat or 0),
        "title": title, "artist": artist, "t": time.time()}
    d = {p: v for p, v in d.items() if os.path.exists(p)}   # drop tracks that were moved away
    _save(d)
    try:
        export(Path(path).parent, d)
    except Exception as e:                                     # never fail a download over this
        print("cue export:", e)


def _outdir(near):
    root = Path(os.environ.get("DJGRAB_OUTPUT") or near)
    out = root / "DJ Grab cues"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _rgb(c):
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def _dur(p):
    try:
        from mutagen import File
        return int(File(p).info.length)
    except Exception:
        return 0


def export(near, d=None):
    d = d if d is not None else _load()
    if APP == "rekordbox":
        rekordbox_xml(_outdir(near) / "DJ Grab (rekordbox).xml", d)
    elif APP == "traktor":
        traktor_nml(_outdir(near) / "DJ Grab (Traktor).nml", d)
    elif APP == "virtualdj":
        virtualdj(d)


def _atomic(path, text):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def rekordbox_xml(out, d):
    rows, keys = [], []
    for i, (p, v) in enumerate(sorted(d.items(), key=lambda kv: kv[1]["t"]), start=1):
        loc = "file://localhost/" + quote(p.replace("\\", "/").lstrip("/"), safe="/:")
        name = v["title"] or Path(p).stem
        r = [f'  <TRACK TrackID="{i}" Name={quoteattr(name)} Artist={quoteattr(v["artist"])} '
             f'Kind="MP3 File" TotalTime="{_dur(p)}" AverageBpm="{v["bpm"]:.2f}" Location={quoteattr(loc)}>']
        if v["bpm"]:
            r.append(f'   <TEMPO Inizio="{v["first"]:.3f}" Bpm="{v["bpm"]:.2f}" Metro="4/4" Battito="1"/>')
        for slot, n, sec, rgb in v["cues"]:
            R, G, B = _rgb(rgb)
            r.append(f'   <POSITION_MARK Name={quoteattr(n)} Type="0" Start="{sec:.3f}" Num="-1"/>')      # memory cue
            r.append(f'   <POSITION_MARK Name={quoteattr(n)} Type="0" Start="{sec:.3f}" Num="{slot}" '
                     f'Red="{R}" Green="{G}" Blue="{B}"/>')                                                # hot cue
        r.append("  </TRACK>")
        rows.append("\n".join(r))
        keys.append(f'    <TRACK Key="{i}"/>')
    _atomic(out, '<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">\n'
            ' <PRODUCT Name="DJ Grab" Version="1" Company="DJ Grab"/>\n'
            f' <COLLECTION Entries="{len(rows)}">\n' + "\n".join(rows) + "\n </COLLECTION>\n"
            ' <PLAYLISTS>\n  <NODE Type="0" Name="ROOT" Count="1">\n'
            f'   <NODE Name="DJ Grab" Type="1" KeyType="0" Entries="{len(keys)}">\n' + "\n".join(keys) +
            "\n   </NODE>\n  </NODE>\n </PLAYLISTS>\n</DJ_PLAYLISTS>\n")


def _traktor_loc(p):
    p = Path(p)
    if WIN:
        vol, parts = p.drive, p.parts[1:-1]
    else:
        vol, parts = "Macintosh HD", p.parts[1:-1]
        if p.parts[1:2] == ("Volumes",) and len(p.parts) > 3:
            vol, parts = p.parts[2], p.parts[3:-1]
    return vol, "/:" + "".join(x + "/:" for x in parts), p.name


def traktor_nml(out, d):
    ents, keys = [], []
    for p, v in sorted(d.items(), key=lambda kv: kv[1]["t"]):
        vol, dir_, file = _traktor_loc(p)
        e = [f'<ENTRY TITLE={quoteattr(v["title"] or Path(p).stem)} ARTIST={quoteattr(v["artist"])}>',
             f'<LOCATION DIR={quoteattr(dir_)} FILE={quoteattr(file)} VOLUME={quoteattr(vol)} VOLUMEID=""></LOCATION>']
        if v["bpm"]:
            e.append(f'<TEMPO BPM="{v["bpm"]:.6f}" BPM_QUALITY="100.000000"></TEMPO>')
            e.append(f'<CUE_V2 NAME="AutoGrid" DISPL_ORDER="0" TYPE="4" START="{v["first"] * 1000:.6f}" '
                     'LEN="0.000000" REPEATS="-1" HOTCUE="-1"></CUE_V2>')
        for slot, n, sec, rgb in v["cues"]:
            e.append(f'<CUE_V2 NAME={quoteattr(n)} DISPL_ORDER="0" TYPE="0" START="{sec * 1000:.6f}" '
                     f'LEN="0.000000" REPEATS="-1" HOTCUE="{slot}"></CUE_V2>')
        e.append("</ENTRY>")
        ents.append("".join(e))
        keys.append(f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY={quoteattr(vol + dir_ + file)}></PRIMARYKEY></ENTRY>')
    _atomic(out, '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n<NML VERSION="19">'
            '<HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>'
            f'<COLLECTION ENTRIES="{len(ents)}">' + "".join(ents) + "</COLLECTION>"
            '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">'
            f'<NODE TYPE="PLAYLIST" NAME="DJ Grab"><PLAYLIST ENTRIES="{len(keys)}" TYPE="LIST" UUID="djgrab">'
            + "".join(keys) + "</PLAYLIST></NODE></SUBNODES></NODE></PLAYLISTS></NML>\n")


def _vdj_running():
    try:
        if WIN:
            o = subprocess.run(["tasklist"], capture_output=True, text=True).stdout
        else:
            o = subprocess.run(["pgrep", "-ix", "virtualdj"], capture_output=True, text=True).stdout
        return "virtualdj" in o.lower() or (not WIN and o.strip() != "")
    except Exception:
        return True                                     # unsure: treat as open and wait


def virtualdj(d):
    """VirtualDJ keeps cues in Documents/VirtualDJ/database.xml and rewrites it on quit, so this only
    edits it while VirtualDJ is closed (a copy of the old file is kept in DJ Grab's support folder)."""
    db = Path.home() / "Documents" / "VirtualDJ" / "database.xml"
    if not db.exists() or _vdj_running():
        return False
    import xml.etree.ElementTree as ET
    tree = ET.parse(db)
    root = tree.getroot()
    songs = {s.get("FilePath"): s for s in root.findall("Song")}
    for p, v in d.items():
        s = songs.get(p)
        if s is None:
            s = ET.SubElement(root, "Song", {"FilePath": p})
        for poi in [x for x in s.findall("Poi") if x.get("Type") in (None, "cue", "beatgrid")]:
            if poi.get("Type") == "beatgrid" or poi.get("Name") in {c[1] for c in v["cues"]} or poi.get("Type") is None:
                s.remove(poi)
        if v["bpm"]:
            ET.SubElement(s, "Poi", {"Pos": f"{v['first']:.6f}", "Type": "beatgrid", "Bpm": f"{60.0 / v['bpm']:.6f}"})
        for slot, n, sec, rgb in v["cues"]:
            ET.SubElement(s, "Poi", {"Name": n, "Pos": f"{sec:.6f}", "Num": str(slot + 1)})
    keep = djgrab.APP_SUPPORT / "VirtualDJ backups"
    keep.mkdir(exist_ok=True)
    shutil.copy2(db, keep / time.strftime("database %Y-%m-%d %H%M%S.xml"))
    tmp = db.with_name(".database.djgrab.xml")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    os.replace(tmp, db)
    return True


if __name__ == "__main__":                          # "python djcues.py": write the library file again now
    print("written" if export(Path.home(), _load()) is not False else "VirtualDJ is open, will write later")
