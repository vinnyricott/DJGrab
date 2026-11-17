"""DJ Grab for Windows.

The same engine as the Mac app (djgrab.py for downloads, djtools.py for intros and cues), with a window built
in Qt. Everything lives in %LOCALAPPDATA%\\DJ Grab: the Python it runs on (venv), the tools (bin), the engine
(engine) and the settings (settings.json). setup.ps1 puts it all there and makes the Start menu shortcut.
"""
import json, os, subprocess, sys, threading, time, urllib.request, zipfile, shutil, tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QFont
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QTabWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QLabel, QListWidget, QListWidgetItem, QProgressBar,
                               QComboBox, QCheckBox, QFileDialog, QDialog, QFormLayout, QDialogButtonBox,
                               QMessageBox, QFrame, QScrollArea, QSizePolicy)
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
except Exception:                     # playback is a nice-to-have
    QMediaPlayer = None

HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DJ Grab"
ENGINE = HOME / "engine"
PY = HOME / "venv" / "Scripts" / "python.exe"
SETTINGS = HOME / "settings.json"
REPO = "vinnyricott/DJGrab"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/win/VERSION"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW: engine runs without a console popping up


def version():
    try:
        return int((ENGINE / "win" / "VERSION").read_text().strip())
    except Exception:
        return 0


def shown(v):
    return str(v) if v < 100 else f"{v // 100}.{v // 10 % 10}.{v % 10}"


DEFAULTS = {"output": str(Path.home() / "Music" / "DJ Grab"), "djApp": "serato", "sortMode": "off",
            "minQuality": 128, "normalizeLUFS": -10.0, "makeIntros": False, "autoUpdate": True,
            "hookTease": "fits", "outroVersions": False, "introBars": 0, "welcomed": False, "preventSleep": True}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(SETTINGS.read_text()))
        except Exception:
            pass

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=1))
        tmp.replace(SETTINGS)


S = Settings()

DJ_APPS = [("serato", "Serato DJ"), ("rekordbox", "Rekordbox"), ("traktor", "Traktor"),
           ("virtualdj", "VirtualDJ"), ("other", "Other / none")]
DJ_NOTES = {
    "serato": "Cue points and beat grids are saved inside each song file and show up in Serato right away.",
    "rekordbox": "Cue points go into \"DJ Grab cues\\DJ Grab (rekordbox).xml\" in your music folder. In Rekordbox, "
                 "choose that file under Preferences > Advanced > Database > rekordbox xml, then open the DJ Grab playlist.",
    "traktor": "Cue points go into \"DJ Grab cues\\DJ Grab (Traktor).nml\" in your music folder. "
               "In Traktor, use File > Import Collection on that file.",
    "virtualdj": "Cue points are added to VirtualDJ's library automatically whenever VirtualDJ is closed.",
    "other": "Songs and intro versions work in any software. Cue points are skipped."}


def engine_env():
    e = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
             DJGRAB_DJAPP=S["djApp"], DJGRAB_HOOK=S["hookTease"], DJGRAB_OUTRO="1" if S["outroVersions"] else "0",
             DJGRAB_OUTPUT=S["output"], DJGRAB_PLAIN="1")
    e["PATH"] = str(HOME / "bin") + os.pathsep + e.get("PATH", "")
    return e


class Runner(QObject):
    """Runs one engine command and hands each JSON event line to the window (on the window's thread)."""
    event = Signal(dict)
    finished = Signal(int, str)

    def __init__(self, args):
        super().__init__()
        self.args, self.proc = args, None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.proc = subprocess.Popen([str(PY)] + self.args, cwd=str(ENGINE), env=engine_env(),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        self.event.emit(json.loads(line))
                    except ValueError:
                        pass
            err = self.proc.stderr.read()
            self.finished.emit(self.proc.wait(), err[-400:])
        except Exception as ex:
            self.finished.emit(1, str(ex))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def _song_key(path):
    import re, unicodedata
    k = unicodedata.normalize("NFC", Path(path).stem).lower()
    k = re.sub(r"\s*[\(\[].*?[\)\]]", "", k)
    return re.sub(r"\s+", " ", k).strip()


def _edit_list(name, add=None, remove=None):
    f = HOME / name
    try:
        keys = json.loads(f.read_text())
    except Exception:
        keys = []
    if add and add not in keys:
        keys.append(add)
    if remove:
        keys = [k for k in keys if k != remove]
    HOME.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(keys))


def use_original(song, intro):
    """No intro for this song: its intro version moves to _To Delete (nothing is deleted), and it won't get one again."""
    k = _song_key(song)
    _edit_list("no_intro.json", add=k)
    _edit_list("force_intro.json", remove=k)
    if intro and os.path.exists(intro):
        if PLAYER.playing == intro:
            PLAYER.stop()
        dest = Path(intro).parent / "_To Delete" / "DJ Grab intros not needed"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(intro, dest / Path(intro).name)


def make_intro(song):
    """An intro for a song DJ Grab thought didn't need one, now and always."""
    k = _song_key(song)
    _edit_list("force_intro.json", add=k)
    _edit_list("no_intro.json", remove=k)


class Player:
    """One song preview at a time, from anywhere in the app."""
    def __init__(self):
        self.playing, self.p = None, None

    def _ready(self):                     # made on first use (it needs the app to exist)
        if self.p is None and QMediaPlayer:
            self.out = QAudioOutput(); self.out.setVolume(0.8)
            self.p = QMediaPlayer(); self.p.setAudioOutput(self.out)
        return self.p is not None

    def toggle(self, path):
        if not self._ready():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path)); return
        if self.playing == path:
            self.stop(); return
        self.p.setSource(QUrl.fromLocalFile(path)); self.p.play(); self.playing = path

    def stop(self):
        if self.p is not None:
            self.p.stop()
        self.playing = None

    def position(self):
        return (self.p.position() / 1000.0, self.p.duration() / 1000.0) if self.p is not None and self.playing else (0.0, 0.0)

    def seek(self, secs):
        if self.p is not None and self.playing:
            self.p.setPosition(int(secs * 1000))


PLAYER = Player()


def play_button(path, label):
    b = QPushButton(f"▶ {label}")
    b.setToolTip(Path(path).name)
    b.clicked.connect(lambda: PLAYER.toggle(path))
    return b


# ------------------------------------------------------------------ first launch
class SetupDialog(QDialog):
    def __init__(self, parent=None, first=True):
        super().__init__(parent)
        self.setWindowTitle("Welcome to DJ Grab" if first else "Settings")
        self.setMinimumWidth(520)
        f = QFormLayout(self)
        intro = QLabel("Set up DJ Grab for this computer. You can change any of this later in Settings.")
        intro.setWordWrap(True)
        f.addRow(intro)
        self.app = QComboBox()
        for k, n in DJ_APPS:
            self.app.addItem(n, k)
        self.app.setCurrentIndex([k for k, _ in DJ_APPS].index(S["djApp"]))
        self.note = QLabel(); self.note.setWordWrap(True); self.note.setStyleSheet("color: gray")
        self.app.currentIndexChanged.connect(lambda _: self.note.setText(DJ_NOTES[self.app.currentData()]))
        self.note.setText(DJ_NOTES[S["djApp"]])
        f.addRow("DJ software", self.app); f.addRow(self.note)
        self.out = QLineEdit(S["output"])
        pick = QPushButton("Choose…"); pick.clicked.connect(self.choose)
        row = QHBoxLayout(); row.addWidget(self.out); row.addWidget(pick)
        f.addRow("Save songs to", row)
        self.sort = QComboBox()
        for k, n in (("off", "Off"), ("simple", "Simple"), ("detailed", "Detailed")):
            self.sort.addItem(n, k)
        self.sort.setCurrentIndex(["off", "simple", "detailed"].index(S["sortMode"]))
        f.addRow("Sort into genre folders", self.sort)
        self.q = QComboBox()
        for k, n in ((0, "Off"), (96, "Basic (96 kbps)"), (128, "Good (128 kbps)"), (160, "High (160 kbps)")):
            self.q.addItem(n, k)
        self.q.setCurrentIndex([0, 96, 128, 160].index(S["minQuality"]) if S["minQuality"] in (0, 96, 128, 160) else 2)
        f.addRow("Minimum quality", self.q)
        self.norm = QComboBox()
        for k, n in ((0.0, "Off"), (-8.0, "Club (-8 LUFS)"), (-10.0, "Standard (-10 LUFS)"), (-12.0, "Quiet (-12 LUFS)")):
            self.norm.addItem(n, k)
        self.norm.setCurrentIndex([0.0, -8.0, -10.0, -12.0].index(S["normalizeLUFS"]) if S["normalizeLUFS"] in (0.0, -8.0, -10.0, -12.0) else 2)
        f.addRow("Normalize volume", self.norm)
        self.intros = QCheckBox("Make intro versions (downloads about 1 GB of extra tools the first time)")
        self.intros.setChecked(S["makeIntros"]); f.addRow(self.intros)
        self.hook = QComboBox()
        for k, n in (("off", "Off"), ("fits", "When it fits"), ("always", "Always")):
            self.hook.addItem(n, k)
        self.hook.setCurrentIndex(["off", "fits", "always"].index(S["hookTease"]))
        f.addRow("Hook tease", self.hook)
        self.outro = QCheckBox("Outro versions (off by default)"); self.outro.setChecked(S["outroVersions"]); f.addRow(self.outro)
        self.upd = QCheckBox("Install updates automatically"); self.upd.setChecked(S["autoUpdate"]); f.addRow(self.upd)
        self.awake = QCheckBox("Keep the computer awake while working (the screen can still turn off)")
        self.awake.setChecked(S["preventSleep"]); f.addRow(self.awake)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Ok).setText("Get Started" if first else "Save")
        bb.accepted.connect(self.save); f.addRow(bb)

    def choose(self):
        d = QFileDialog.getExistingDirectory(self, "Where should your songs go?", self.out.text())
        if d:
            self.out.setText(d)

    def save(self):
        S.update(djApp=self.app.currentData(), output=self.out.text(), sortMode=self.sort.currentData(),
                 minQuality=self.q.currentData(), normalizeLUFS=self.norm.currentData(),
                 makeIntros=self.intros.isChecked(), hookTease=self.hook.currentData(),
                 outroVersions=self.outro.isChecked(), autoUpdate=self.upd.isChecked(),
                 preventSleep=self.awake.isChecked(), welcomed=True)
        S.save()
        Path(S["output"]).mkdir(parents=True, exist_ok=True)
        self.accept()


# ------------------------------------------------------------------ Grab
class Row(QFrame):
    def __init__(self, link):
        super().__init__()
        self.link, self.path = link, None
        self.setFrameShape(QFrame.StyledPanel)
        v = QVBoxLayout(self); v.setContentsMargins(8, 6, 8, 6)
        top = QHBoxLayout()
        self.name = QLabel(link); self.name.setFont(QFont("", 10, QFont.Bold))
        self.btns = QHBoxLayout()
        top.addWidget(self.name, 1); top.addLayout(self.btns)
        v.addLayout(top)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False); self.bar.setFixedHeight(6)
        v.addWidget(self.bar)
        self.stage = QLabel("Waiting"); self.stage.setStyleSheet("color: gray")
        v.addWidget(self.stage)

    def set_done(self, path, detail):
        self.path = path
        self.bar.setValue(100); self.bar.hide()
        self.stage.setText(detail)
        if path:
            self.btns.addWidget(play_button(path, "Play"))
            b = QPushButton("Show"); b.clicked.connect(lambda: subprocess.run(["explorer", "/select,", path]))
            self.btns.addWidget(b)


class GrabTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.queue, self.cur, self.runner, self.allow_low = win, [], None, None, set()
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.box = QLineEdit(); self.box.setPlaceholderText("Paste a YouTube or SoundCloud link, or type a song name…")
        self.box.returnPressed.connect(self.grab)
        go = QPushButton("Grab"); go.setDefault(True); go.clicked.connect(self.grab)
        row.addWidget(self.box, 1); row.addWidget(go)
        v.addLayout(row)
        self.list = QVBoxLayout(); self.list.addStretch(1)
        holder = QWidget(); holder.setLayout(self.list)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setWidget(holder)
        v.addWidget(sc, 1)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasText() or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        t = "\n".join(u.toString() for u in md.urls()) if md.hasUrls() else md.text()
        for line in t.splitlines():
            self.add(line)

    def grab(self):
        t = self.box.text().strip()
        if t:
            self.add(t); self.box.clear()

    def add(self, link):
        link = link.strip()
        if not link:
            return
        if not link.lower().startswith(("http://", "https://", "www.", "search:")):
            link = "search:" + link            # a song name: DJ Grab finds the right upload (spelling can be off)
        r = Row(link)
        if link.startswith("search:"):
            r.name.setText(link[7:])
        self.list.insertWidget(0, r)
        self.queue.append(r)
        self.next()

    def next(self):
        if self.runner or not self.queue:
            return
        self.cur = self.queue.pop(0)
        args = ["djgrab.py", self.cur.link, "--out", S["output"], "--json", "--plain"]
        if S["sortMode"] != "off":
            args += ["--sort", S["sortMode"]]
        if S["normalizeLUFS"] < 0:
            args += ["--normalize", str(S["normalizeLUFS"])]
        if S["minQuality"] > 0:
            args += ["--min-kbps", str(S["minQuality"])]
        if self.cur.link in self.allow_low:
            args.append("--allow-low")
        self.cur.stage.setText("Starting")
        self.runner = Runner(args)
        self.runner.event.connect(self.on_event)
        self.runner.finished.connect(self.on_finished)
        self.runner.start()

    def on_event(self, e):
        r, ev = self.cur, e.get("event")
        if not r:
            return
        if ev == "item_start":
            r.name.setText(e.get("name", r.link))
        elif ev == "progress":
            r.stage.setText(e.get("stage", "")); r.bar.setValue(int(e.get("pct", 0) or 0))
            if e.get("name"):
                r.name.setText(e["name"])
        elif ev == "done":
            r.name.setText(f"{e.get('artist', '')} - {e.get('title', '')}")
            r.set_done(e.get("path"), e.get("source") or "Ready")
            if S["makeIntros"] and e.get("path"):
                self.win.intros.queue_intro(e["path"])
        elif ev == "skipped":
            r.set_done(e.get("path"), "Already have it")
        elif ev == "lowquality":
            r.stage.setText(e.get("message", "Poor quality")); r.bar.hide()
            b = QPushButton("Download anyway")
            link = e.get("link") or r.link
            def again():
                self.allow_low.add(link); b.setEnabled(False); self.add(link)
            b.clicked.connect(again); r.btns.addWidget(b)
        elif ev == "error":
            r.stage.setText(e.get("message", "Error")); r.stage.setStyleSheet("color: #e05"); r.bar.hide()
        elif ev == "info" and e.get("message"):
            r.stage.setText(e["message"])

    def on_finished(self, code, err):
        if code and self.cur and self.cur.bar.isVisible():
            self.cur.stage.setText("Stopped: " + (err.strip().splitlines() or ["error"])[-1])
            self.cur.bar.hide()
        self.runner, self.cur = None, None
        self.next()


# ------------------------------------------------------------------ Intros & Cues
class IntrosTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.runner, self.waiting = win, None, []
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.folder = QLineEdit(S["output"]); self.folder.setReadOnly(True)
        pick = QPushButton("Folder…"); pick.clicked.connect(self.choose)
        self.start_b = QPushButton("Start"); self.start_b.clicked.connect(self.start_folder)
        self.pause_b = QPushButton("Pause"); self.pause_b.clicked.connect(self.pause); self.pause_b.hide()
        self.songs_b = QPushButton("Songs"); self.songs_b.clicked.connect(self.list_songs)
        self.redo_b = QPushButton("Redo all"); self.redo_b.clicked.connect(lambda: self.start_folder(redo=True))
        self.redo_b.setToolTip("Run every song in this folder again with the newest rules, including songs already done")
        for w in (self.folder, pick, self.start_b, self.pause_b, self.redo_b, self.songs_b):
            top.addWidget(w)
        v.addLayout(top)
        self.cur = QLabel(""); self.cur.setFont(QFont("", 10, QFont.Bold))
        self.bar = QProgressBar(); self.bar.setTextVisible(False); self.bar.setFixedHeight(6); self.bar.hide()
        self.stage = QLabel(""); self.stage.setStyleSheet("color: gray")
        self.next_l = QLabel(""); self.next_l.setStyleSheet("color: gray"); self.next_l.setWordWrap(True)
        for w in (self.cur, self.bar, self.stage, self.next_l):
            v.addWidget(w)
        self.search = QLineEdit(); self.search.setPlaceholderText("Find a song in your folder to run…")
        self.search.textChanged.connect(self.find)
        v.addWidget(self.search)
        self.hits = QListWidget(); self.hits.setMaximumHeight(140); self.hits.hide()
        self.hits.itemDoubleClicked.connect(lambda it: self.queue_intro(it.data(Qt.UserRole)))   # run / queue
        self.hits.itemClicked.connect(lambda it: PLAYER.toggle(it.data(Qt.UserRole)))            # listen
        v.addWidget(self.hits)
        self.results = QVBoxLayout(); self.results.addStretch(1)
        holder = QWidget(); holder.setLayout(self.results)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setWidget(holder)
        v.addWidget(sc, 1)
        self._all = None

    def choose(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to run", self.folder.text())
        if d:
            self.folder.setText(d); self._all = None

    def songs(self):
        if self._all is None:
            out = []
            for dp, dn, fn in os.walk(self.folder.text()):
                dn[:] = [d for d in dn if not d.startswith(("_", "."))]
                out += [os.path.join(dp, f) for f in fn if f.lower().endswith(".mp3") and not f.endswith((" (Intro).mp3", " (Outro).mp3"))]
            self._all = sorted(out, key=lambda p: Path(p).name.lower())
        return self._all

    def find(self, q):
        words = [w for w in q.lower().split() if len(w) >= 2]
        self.hits.clear()
        if not words:
            self.hits.hide(); return
        for p in [p for p in self.songs() if all(w in Path(p).stem.lower() for w in words)][:30]:
            it = QListWidgetItem(Path(p).stem + ("   (click: listen · double-click: queue)" if self.runner else "   (click: listen · double-click: run)"))
            it.setData(Qt.UserRole, p); self.hits.addItem(it)
        self.hits.show()

    def list_songs(self):
        self.hits.clear()
        for p in self.songs():
            it = QListWidgetItem(Path(p).stem); it.setData(Qt.UserRole, p); self.hits.addItem(it)
        self.hits.setMaximumHeight(320); self.hits.show()

    def queue_intro(self, path, force=False):
        if self.runner:
            if path not in self.waiting:
                self.waiting.append(path)
            self.next_l.setText("Up next: " + ", ".join(Path(p).stem for p in self.waiting))
            return
        self.run(["djtools.py", "intro", path, "--json", "--bars", str(S["introBars"])], Path(path).stem)

    def start_folder(self, redo=False):
        args = ["djtools.py", "batch", "--root", self.folder.text(), "--bars", str(S["introBars"]), "--json"]
        if redo:
            args.append("--redo")
        if not S["makeIntros"]:
            args.append("--no-intro")
        self.run(args, Path(self.folder.text()).name)

    def run(self, args, label):
        self.cur.setText(f"Starting: {label}…"); self.bar.setRange(0, 0); self.bar.show(); self.stage.setText("")
        self.start_b.hide(); self.redo_b.hide(); self.pause_b.show()
        self.runner = Runner(args)
        self.runner.event.connect(self.on_event)
        self.runner.finished.connect(self.on_finished)
        self.runner.start()

    def pause(self):
        if self.runner:
            self.runner.stop()

    def on_event(self, e):
        ev = e.get("event")
        if ev == "item_start":
            self.cur.setText(e.get("name", "")); self.bar.setRange(0, 100); self.bar.setValue(0)
        elif ev == "progress":
            self.bar.setRange(0, 100); self.bar.setValue(int(e.get("pct", 0) or 0)); self.stage.setText(e.get("stage", ""))
        elif ev in ("done", "skipped", "error"):
            self.add_result(e)

    def add_result(self, e):
        f = QFrame(); f.setFrameShape(QFrame.StyledPanel)
        h = QHBoxLayout(f)
        t = QVBoxLayout()
        n = QLabel(e.get("name", "")); n.setFont(QFont("", 10, QFont.Bold)); t.addWidget(n)
        bits = []
        if e.get("cues"):
            bits.append(" · ".join(e["cues"]))
        if e.get("intro"):
            bits.append("Intro version: " + " · ".join(e.get("intro_cues") or []) if e.get("intro_cues") else "Intro version")
        if e.get("note"):
            bits.append(e["note"])
        if e.get("message"):
            bits.append(e["message"])
        d = QLabel("   —   ".join(bits)); d.setWordWrap(True); d.setStyleSheet("color: gray"); t.addWidget(d)
        h.addLayout(t, 1)
        song = e.get("path")
        if song and os.path.exists(song):
            h.addWidget(play_button(song, "Original"))
        if e.get("intro") and os.path.exists(e["intro"]):
            h.addWidget(play_button(e["intro"], "Intro"))
            u = QPushButton("Use original")
            u.setToolTip("Remove this intro version (moved to _To Delete) and keep only the original")
            def _orig(_=False, s_=song, i_=e["intro"], f_=f):
                use_original(s_, i_); f_.hide()
            u.clicked.connect(_orig); h.addWidget(u)
        elif song and os.path.exists(song) and e.get("event") == "done" and "intro" not in Path(song).stem.lower():
            mk = QPushButton("Make intro")
            mk.setToolTip("DJ Grab thought this song didn't need an intro. Make one anyway (and always for this song)")
            def _make(_=False, s_=song, f_=f):
                make_intro(s_); f_.hide(); self.queue_intro(s_, force=True)
            mk.clicked.connect(_make); h.addWidget(mk)
        self.results.insertWidget(0, f)

    def on_finished(self, code, err):
        self.runner = None
        self.bar.hide(); self.pause_b.hide(); self.start_b.show(); self.redo_b.show()
        self.cur.setText("Done" if code == 0 else "Paused" if code in (1, 15, -15) and not err.strip() else "Stopped: " + (err.strip().splitlines() or [""])[-1])
        self.stage.setText("")
        if self.waiting:
            p = self.waiting.pop(0)
            self.next_l.setText("Up next: " + ", ".join(Path(q).stem for q in self.waiting) if self.waiting else "")
            self.queue_intro(p)


# ------------------------------------------------------------------ window
class Window(QMainWindow):
    # worker threads talk to the window through signals (a timer started on another thread never fires)
    latest_found = Signal()
    update_msg = Signal(str)
    quit_now = Signal()

    def __init__(self):
        super().__init__()
        self.latest_found.connect(self.show_update)
        self.update_msg.connect(lambda m: self.update_l.setText(m))
        self.quit_now.connect(QApplication.quit)
        self.setWindowTitle("DJ Grab")
        self.resize(760, 640)
        ico = ENGINE / "win" / "DJGrab.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        w = QWidget(); v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.update_l = QLabel(""); self.update_b = QPushButton("Install update"); self.update_b.hide()
        self.update_b.clicked.connect(self.install_update)
        stop = QPushButton("■ Stop sound"); stop.clicked.connect(PLAYER.stop)
        from PySide6.QtWidgets import QSlider
        self.seek = QSlider(Qt.Horizontal); self.seek.setMinimumWidth(160); self.seek.setRange(0, 1000); self.seek.hide()
        self.seek_l = QLabel(""); self.seek_l.setStyleSheet("color: gray"); self.seek_l.hide()
        self.seek.sliderReleased.connect(lambda: PLAYER.seek(self.seek.value() / 1000 * PLAYER.position()[1]))
        tick = QTimer(self); tick.timeout.connect(self.update_seek); tick.start(250)
        awake = QTimer(self); awake.timeout.connect(self.keep_awake); awake.start(5000)
        gear = QPushButton("Settings"); gear.clicked.connect(lambda: SetupDialog(self, first=False).exec())
        bar.addWidget(self.update_l, 1); bar.addWidget(self.update_b); bar.addWidget(self.seek_l); bar.addWidget(self.seek)
        bar.addWidget(stop); bar.addWidget(gear)
        v.addLayout(bar)
        tabs = QTabWidget()
        self.grab = GrabTab(self); self.intros = IntrosTab(self)
        tabs.addTab(self.grab, "Grab"); tabs.addTab(self.intros, "Intros && Cues")
        v.addWidget(tabs, 1)
        foot = QLabel(f"v{shown(version())}"); foot.setStyleSheet("color: gray"); v.addWidget(foot)
        self.setCentralWidget(w)
        self.latest = 0
        QTimer.singleShot(3000, self.check_update)
        t = QTimer(self); t.timeout.connect(self.check_update); t.start(3 * 3600 * 1000)

    def keep_awake(self):
        """While songs download or intros are made, Windows doesn't go to sleep on its own (Settings)."""
        busy = bool(self.grab.runner or self.intros.runner) and S["preventSleep"]
        if busy == getattr(self, "_awake", False):
            return
        self._awake = busy
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if busy else 0))
        except Exception:
            pass                       # not on Windows (tests)

    def update_seek(self):
        pos, dur = PLAYER.position()
        on = bool(PLAYER.playing) and dur > 0
        self.seek.setVisible(on); self.seek_l.setVisible(on)
        if on:
            if not self.seek.isSliderDown():
                self.seek.setValue(int(pos / dur * 1000))
            self.seek_l.setText(f"{int(pos) // 60}:{int(pos) % 60:02d} / {int(dur) // 60}:{int(dur) % 60:02d}")

    def check_update(self):
        def work():
            try:
                with urllib.request.urlopen(VERSION_URL, timeout=10) as r:
                    self.latest = int(r.read().decode().strip())
            except Exception:
                return
            self.latest_found.emit()
        threading.Thread(target=work, daemon=True).start()

    def show_update(self):
        if self.latest > version():
            self.update_l.setText(f"Update v{shown(self.latest)} is ready · You have v{shown(version())}")
            self.update_b.show()
            if S["autoUpdate"] and not self.grab.runner and not self.intros.runner:
                self.install_update()

    def install_update(self):
        self.update_l.setText("Installing update… DJ Grab will reopen by itself")
        self.update_b.setEnabled(False)

        def work():
            try:
                tmp = Path(tempfile.mkdtemp())
                urllib.request.urlretrieve(ZIP_URL, tmp / "dj.zip")
                with zipfile.ZipFile(tmp / "dj.zip") as z:
                    z.extractall(tmp)
                src = next(tmp.glob("DJGrab-*"))
                new = HOME / "engine.new"
                shutil.rmtree(new, ignore_errors=True)
                shutil.copytree(src, new, ignore=shutil.ignore_patterns("app-src", ".git*", "*.md"))
                old = HOME / "engine.old"
                shutil.rmtree(old, ignore_errors=True)
                if ENGINE.exists():
                    ENGINE.rename(old)
                new.rename(ENGINE)
                for f in ("logs",):                              # keep the engine's own logs
                    if (old / f).exists() and not (ENGINE / f).exists():
                        shutil.copytree(old / f, ENGINE / f)
                subprocess.Popen([str(PY.with_name("pythonw.exe")), str(ENGINE / "win" / "DJGrab.pyw")],
                                 creationflags=NO_WINDOW)
                self.quit_now.emit()
            except Exception as ex:
                self.update_msg.emit(f"Update failed: {ex}")
        threading.Thread(target=work, daemon=True).start()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DJ Grab")
    if not S["welcomed"]:
        if SetupDialog(first=True).exec() != QDialog.Accepted:
            return
    w = Window(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
