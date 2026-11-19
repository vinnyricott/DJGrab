"""DJ Grab self-tests: engine rules and audio building on made-up songs, the cue exports, the name helpers,
and the Windows window (run headless). No internet and no stem models needed.
    python3 tests/run_tests.py
"""
import json, os, sys, tempfile, traceback, xml.dom.minidom
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import djgrab, djtools as d, djcues   # noqa: E402

SR = d.SR
d.ensure_hq = lambda: False
d.log = lambda *a, **k: None
RESULTS = []


def check(name, cond, info=""):
    RESULTS.append((name, bool(cond), info))


def song(nb=40, bar=2.0, hit_ramp=88, early=0.0, pad=0.0, vox_from=4, vox_to=30, quiet_bars=0, late_from=None):
    n = int(nb * bar * SR); t = np.arange(n) / SR
    drums = np.zeros((n, 2), "float32")
    for b in np.arange(0.5 - early, nb * bar - 1, bar / 4):
        if b < quiet_bars * bar:
            continue
        sh = 0.02 if late_from is not None and b >= late_from * bar else 0.0
        i = int((b + sh) * SR)
        env = (np.hanning(6000)[3000:] * np.minimum(1, np.arange(3000) / hit_ramp)).astype("float32")
        drums[i:i + 3000] += env[:, None] * 0.5
    vox = np.zeros((n, 2), "float32")
    for k in range(vox_from, vox_to):
        a = int(k * bar * SR); m = int(1.5 * SR)
        vox[a:a + m] += (0.2 * np.sin(2 * np.pi * 440 * t[a:a + m]))[:, None]
    bg = (pad * np.sin(2 * np.pi * 55 * t))[:, None].repeat(2, 1).astype("float32")
    orig = drums + vox + bg
    voc = np.array([0.5 if vox_from <= i < vox_to else 0.0 for i in range(nb)])
    drm = np.array([0.5 if i >= quiet_bars else 0.0 for i in range(nb)])
    info = dict(nb=nb, sb=4, voc=voc, drm=drm, v90=0.5, loud=10, beat=np.ones(nb), full=1, anchor=4, top="Hip Hop",
                every=np.ones(nb) * 0.3, bpm=120.0, drift=0.0)
    return orig, {"vocals": vox, "drums": drums}, np.arange(nb) * bar, info


def intro_case(name, n_bars=8, **kw):
    top = kw.pop("top", "Hip Hop"); dance = kw.pop("dance", False); bpm = kw.pop("bpm", 120.0)
    orig, stems, bars, info = song(**kw)
    info.update(top=top, dance=dance, bpm=bpm)
    a, gb, style, cue, off = d.build_intro_bars(orig, stems, bars, dict(info), n_bars)
    sb_t = d._snap_bars(orig, bars)[4]
    flags = d.check_intro(a, gb, off, 120.0, orig[int(sb_t * SR):])
    check(f"intro {name}: builds", len(a) > 0 and len(gb) == n_bars, f"{len(gb)} bars")
    check(f"intro {name}: passes the quality check", not any(f in d.SERIOUS for f in flags), ", ".join(flags))
    m = np.abs(a[:int(0.0015 * SR)]).max()          # the 2 ms before the hit's attack
    check(f"intro {name}: starts from (near) silence", m < 0.25 * np.abs(a[:int(0.05 * SR)]).max(), f"{m:.2f}")
    o = int(off * SR)
    s = orig[int(sb_t * SR):]
    lag = int(np.argmax(np.correlate(a[o:o + SR, 0], s[:SR, 0], "full"))) - (SR - 1)
    check(f"intro {name}: song comes in on time", abs(lag) < int(0.006 * SR), f"{lag * 1000 / SR:.1f} ms")
    return style


def run():
    # --- audio building
    intro_case("hip hop")
    intro_case("house (drums + bass)", top="House", dance=True)
    intro_case("held note under the hits", pad=0.25)
    intro_case("hits that start early", early=0.008)
    intro_case("16 bars", n_bars=16)
    st = intro_case("soft pop (no tension build)", top="Pop", bpm=90.0)
    check("soft songs skip the tension build", "tension" not in st, st)

    # bars joined: length kept, the 1 keeps its attack
    parts = [np.ones((1000, 2), "float32") * 0.5 for _ in range(3)]
    j = d._xjoin(parts)
    check("bars join without losing length", len(j) == 3000)
    check("bars join without softening the downbeat", float(np.abs(j[1000 + 20]).max()) > 0.45)

    # bar lines on the hit's attack
    orig, _, bars, _ = song()
    snapped = d._snap_bars(orig, bars)
    check("bar lines sit on the attack (not the peak)", np.all(np.abs(snapped[1:20] - bars[1:20]) < 0.006),
          str(np.round(snapped[1:4] - bars[1:4], 4)))

    # (half-beat grid move is switched off)
    orig, _, bars, _ = song()
    nb, _ = d.fix_half_beat(bars, {"markers": []}, orig, 120.0, "t")
    check("correct grid left alone", np.allclose(nb, bars))

    # quality gate catches the three problems
    orig, stems, bars, info = song()
    a, gb, style, cue, off = d.build_intro_bars(orig, stems, bars, dict(info), 8)
    s = orig[int(d._snap_bars(orig, bars)[4] * SR):]
    cut = int(0.006 * SR)                         # an intro that starts in the middle of its first hit
    bad = a[cut:]
    check("gate: cut-off start caught", "cut-off start" in d.quality_gate(bad, gb, off - cut / SR, 120.0, s))
    o = int(off * SR)
    sh = np.concatenate([a[:o], np.zeros((int(0.03 * SR), 2), "float32"), a[o:]])
    check("gate: beat jump caught", "beat jumps" in d.quality_gate(sh, gb, off, 120.0, s))
    cl = a.copy()
    for k in range(16):
        i = o + int((k * 0.5 + 0.25) * SR)
        cl[i:i + 3000] += np.hanning(6000)[3000:, None].astype("float32") * 0.5
    check("gate: clashing drums caught", "drums clash" in d.quality_gate(cl, gb, off, 120.0, s))

    # mid-intro jump: bars 5-8 a third of a beat late get caught, then slid back onto the beat
    beat_ = 0.5; nbs = int(2.0 * SR); sl = int(0.17 * SR)
    jm = a.copy()
    gb_i = int(round(float(gb[4]) * SR))
    jm[gb_i:o] = a[gb_i - sl:o - sl]
    check("gate: beat jump inside the intro caught", "beat jumps" in d.quality_gate(jm, gb, off, 120.0, s))
    rp, what, mv = d.repair_intro(jm, gb, off, 120.0)
    check("intro jump repaired", bool(what) and "beat jumps" not in d.quality_gate(rp, gb, off, 120.0, s), str(what))
    check("clean intro left untouched by repair", not d.repair_intro(a, gb, off, 120.0)[1])
    late = np.concatenate([a[:o], np.zeros((int(0.12 * SR), 2), "float32"), a[o:]])
    rp, what, mv = d.repair_intro(late, gb, off, 120.0)
    check("song coming in late slid onto the intro's beat", mv > 0.1 and "beat jumps" not in d.quality_gate(rp, gb, off, 120.0, s), str(what))
    hole = a.copy(); hb = int(round((float(gb[3]) + 0.5 - 0.02) * SR))
    hole[hb:hb + int(0.5 * SR)] = 0
    rp, what, mv = d.repair_intro(hole, gb, off, 120.0)
    check("a beat missing from the intro filled back in", any("missing hit" in w for w in what), str(what))

    # a song with its own short intro (6 bars of beat before the vocals) gets it extended to 8, played as recorded
    orig_, stems_, bars_, info_ = song(vox_from=10)
    a_, gb_, st_, cue_, off_ = d.build_intro_bars(orig_, stems_, bars_, dict(info_), 8)
    sbt_ = d._snap_bars(orig_, bars_)[4]
    check("own 6-bar intro extended: 2 bars added in front", "extended to 8 bars" in st_ and len(gb_) == 2, st_)
    o_ = int(off_ * SR); s_ = orig_[int(sbt_ * SR):]
    check("own intro extended: the song plays from its real first bar, untouched",
          float(np.abs(a_[o_ + 2000:o_ + 4 * SR] - s_[2000:4 * SR]).max()) < 0.02)
    fl_ = d.check_intro(a_, gb_, off_, 120.0, s_)
    check("own intro extended: passes the quality check", not any(f in d.SERIOUS for f in fl_), ", ".join(fl_))

    # a short own opening that isn't extended is replaced: after the 8 bars the song comes in on its vocals
    i2_ = dict(info_, no_extend=True)
    a2_, gb2_, st2_, _, off2_ = d.build_intro_bars(orig_, stems_, bars_, i2_, 8)
    check("8 bars, then straight into the vocals (own opening replaced)",
          i2_.get("song_from") == 10 and len(gb2_) == 8 and "comes in on its vocals" in st2_, st2_)

    # snare build: the song's own snare rolls on the exact grid over the last 2 beats (8ths, then 16ths)
    n_ = SR * 2; q_ = n_ // 4
    sb_ = np.zeros((n_, 2), "float32")
    for b_ in (1, 3):
        t_ = b_ * q_ + int(0.01 * SR)
        sb_[t_:t_ + 2000] += (np.random.default_rng(b_).standard_normal((2000, 2)) * np.linspace(1, 0, 2000)[:, None] * 0.5).astype("float32")
    tb_ = d.tension_bar(sb_, np.zeros_like(sb_), [sb_])
    env_ = np.convolve(np.abs(tb_).mean(axis=1), np.ones(100) / 100, "same")
    on_ = [round((i - n_ // 2) / (n_ / 16), 2) for i in range(n_ // 2, n_) if env_[i] > 0.02 and env_[i - 1] <= 0.02]
    check("snare build lands on the grid (8ths then 16ths, with the song's own feel)",
          len(on_) == 6 and all(abs(o - round(o) - 0.07) < 0.03 for o in on_) and [round(o) for o in on_] == [0, 2, 4, 5, 6, 7], str(on_))
    check("snare build has no silence before the song", float(np.abs(tb_[-n_ // 16:]).max()) > 0.01)

    # handover lock: a song whose beat comes in late gets moved onto the beat
    orig, stems, bars, info = song(late_from=4)
    a, gb, style, cue, off = d.build_intro_bars(orig, stems, bars, dict(info), 8)
    o = int(off * SR); s2 = orig[int(d._snap_bars(orig, bars)[4] * SR):]
    lag = int(np.argmax(np.correlate(a[o:o + SR, 0], s2[:SR, 0], "full"))) - (SR - 1)
    check("a song whose beat comes in late still lands on time", abs(lag) < int(0.006 * SR), f"{lag * 1000 / SR:.1f} ms")

    # needs-intro rules
    bar = 2.0
    check("vocals from bar 1 need an intro", d.needs_intro({"Start": 0.0, "Vocals": 0.5, "_vox_share": 0.6, "_vox_run": 0.6}, bar))
    check("clean 8 bars need none", not d.needs_intro({"Start": 0.0, "Vocals": 20.0, "_intro_beat": 0.9}, bar))
    check("house: chants over the beat need none",
          not d.needs_intro({"Start": 0.0, "Vocals": 1.0, "_vox_share": 0.2, "_vox_run": 0.3, "_intro_beat": 0.5}, bar, dance=True))
    check("house: percussion intro needs none",
          not d.needs_intro({"Start": 0.0, "Vocals": 30.0, "_intro_beat": 0.2}, bar, dance=True))
    check("house: no beat at all needs one", d.needs_intro({"Start": 0.0, "Vocals": 30.0, "_intro_beat": 0.05}, bar, dance=True))
    check("every intro is 8 bars", d.auto_bars("/V/House/Tech House/x.mp3") == 8 and d.auto_bars("/V/Pop/Pop/x.mp3") == 8)

    # smart re-check after updates
    st_ = {"done": {"/V/House/Afro House/a.mp3": "done@30", "/V/Pop/Pop/b.mp3": "done@30"},
           "rule": {"/V/House/Afro House/a.mp3": 5, "/V/Pop/Pop/b.mp3": d.INTRO_RULE},
           "kind": {"/V/House/Afro House/a.mp3": "intro", "/V/Pop/Pop/b.mp3": "intro"}}
    check("old intro re-checked after an update", d.needs_recheck(st_, "/V/House/Afro House/a.mp3"))
    check("up-to-date intro skipped", not d.needs_recheck(st_, "/V/Pop/Pop/b.mp3"))

    # hook tease placement
    orig, stems, bars, info = song()
    intro = orig[:int(16 * SR)].copy(); lens = [int(2 * SR)] * 8
    d.HOOK_MODE = "always"; info["chorus_bar"] = 10
    _, why = d.hook_tease(intro, lens, orig, stems, np.append(bars, len(orig) / SR), info, "the song's beat", 4)
    check("hook tease: 4 bars of beat before the song", why == "bars 3-4", why)
    d.HOOK_MODE = "fits"

    # names and folders
    check("holiday song goes to Holidays", djgrab.holiday_folder("All I Want for Christmas Is You") == "Holidays/Christmas")
    check("'Santa Monica' is not a holiday song", djgrab.holiday_folder("Santa Monica") is None)
    check("plain-English error messages exist", "internet" in djgrab.friendly_error("getaddrinfo failed").lower())

    # cue exports parse
    tmp = Path(tempfile.mkdtemp())
    data = {"/Users/v/Music/A & B.mp3": {"cues": [[0, "Start", 1.5, 0x00CC00], [1, "Halfway", 9.0, 0x00CCCC]],
                                          "bpm": 124.0, "first": 0.2, "title": "A <x>", "artist": "B & C", "t": 1}}
    djcues.rekordbox_xml(tmp / "r.xml", data); djcues.traktor_nml(tmp / "t.nml", data)
    for f in ("r.xml", "t.nml"):
        try:
            xml.dom.minidom.parse(str(tmp / f)); ok = True
        except Exception as ex:
            ok = str(ex)
        check(f"{f} export is valid", ok is True, str(ok))
    djcues.WIN = True
    vol, dir_, file = djcues._traktor_loc("C:\\Users\\v\\Music\\Song.mp3")
    djcues.WIN = False
    check("Traktor path on Windows", file.endswith("Song.mp3"), f"{vol}|{dir_}|{file}")

    # Windows engine helpers (simulated)
    check("crate lock works", (lambda: [djgrab._CrateLock().__enter__().__exit__()] and True)())


def run_windows_ui():
    """The Windows window, headless, against this engine (python3 stands in for the Windows Python)."""
    import importlib.util, subprocess, time, shutil
    root = Path(tempfile.mkdtemp()); home = root / "DJ Grab"
    (home / "venv" / "Scripts").mkdir(parents=True); (home / "bin").mkdir()
    os.symlink(sys.executable, home / "venv" / "Scripts" / "python.exe")
    shutil.copytree(HERE, home / "engine", ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t):
            os.symlink(shutil.which(t), home / "bin" / t)
    os.environ["LOCALAPPDATA"] = str(root); os.environ["QT_QPA_PLATFORM"] = "offscreen"
    _P = subprocess.Popen
    def P(*a, **k):
        k.pop("creationflags", None); return _P(*a, **k)
    subprocess.Popen = P
    spec = importlib.util.spec_from_file_location("winapp", str(home / "engine" / "win" / "DJGrab.pyw"),
                                                  loader=__import__("importlib.machinery").machinery.SourceFileLoader("winapp", str(home / "engine" / "win" / "DJGrab.pyw")))
    app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
    from PySide6.QtWidgets import QApplication
    qa = QApplication.instance() or QApplication([])
    music = root / "music"; music.mkdir()
    dlg = app.SetupDialog(first=True); dlg.out.setText(str(music)); dlg.save()
    check("win: settings saved", json.loads((home / "settings.json").read_text())["output"] == str(music))
    w = app.Window(); w.show()
    check("win: version shown 1.x.y style", app.shown(113) == "1.1.3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=30", "-b:a", "320k",
                    str(music / "Test Artist - Test Song.mp3")])
    w.intros.folder.setText(str(music)); w.intros._all = None
    w.intros.find("test song"); check("win: song search finds it", w.intros.hits.count() == 1)
    w.intros.list_songs(); check("win: Songs list", w.intros.hits.count() == 1)
    w.grab.add("zzzz qqq nothing")
    t0 = time.time()
    while time.time() - t0 < 60 and (w.grab.runner or w.grab.queue):
        qa.processEvents(); time.sleep(0.1)
    r = w.grab.list.itemAt(0).widget()
    check("win: a search runs and reports back", r.stage.text() not in ("", "Starting", "Waiting"), r.stage.text())
    s_ = str(music / "Test Artist - Test Song.mp3")
    ip = str(music / "Test Artist - Test Song (Intro).mp3"); shutil.copy(s_, ip)
    app.use_original(s_, ip)
    check("win: Use original moves the intro to _To Delete", not os.path.exists(ip) and
          (music / "_To Delete" / "DJ Grab intros not needed" / Path(ip).name).exists())
    app.make_intro(s_)
    check("win: Make intro remembered", "test artist - test song" in json.loads((home / "force_intro.json").read_text())
          and "test artist - test song" not in json.loads((home / "no_intro.json").read_text()))
    w.update_seek(); check("win: seek bar hidden when nothing plays", not w.seek.isVisible())


if __name__ == "__main__":
    for f in (run, run_windows_ui):
        try:
            f()
        except Exception:
            RESULTS.append((f.__name__ + " crashed", False, traceback.format_exc()[-800:]))
    bad = [r for r in RESULTS if not r[1]]
    for name, ok, info in RESULTS:
        print(("PASS " if ok else "FAIL ") + name + (f"   ({info})" if info and not ok else ""))
    print(f"\n{len(RESULTS) - len(bad)} passed, {len(bad)} failed")
    sys.exit(1 if bad else 0)
