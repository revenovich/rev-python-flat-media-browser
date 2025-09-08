# put this at the very top of video_player.py, before "import mpv"
import os, sys
from shutil import which

if sys.platform.startswith("win"):
    print("Configuring mpv DLL search path...")
    candidates = []

    # 1) If user has MPV_DLL_PATH set, prefer it
    dll_env = os.environ.get("MPV_DLL_PATH", "")
    if dll_env:
        candidates.append(os.path.dirname(dll_env))

    # 2) Folder containing mpv.exe (if on PATH)
    exe = which("mpv")
    if exe:
        print(f"Found mpv.exe at {exe}")
        candidates.append(os.path.dirname(exe))

    # 3) Common install locations
    candidates += [
        r"C:\Program Files\mpv",
        r"C:\Program Files (x86)\mpv",
    ]

    for d in candidates:
        if not d:
            continue
        if (os.path.exists(os.path.join(d, "mpv-2.dll")) or
            os.path.exists(os.path.join(d, "libmpv-2.dll"))):
            try:
                # Python 3.8+ preferred way
                print(f"Adding {d} to DLL search path")
                os.add_dll_directory(d)
            except AttributeError:
                print(f"Prepending {d} to PATH for DLL search")
                # Fallback for older Pythons
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            break

import mpv
import time, threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path

class VideoPanel(ttk.Frame):
    """
    mpv-based embedded player:
      • Single mpv instance embedded into a Tk Frame
      • Load on select (paused+muted), unmute only after first video frame
      • Smooth, accurate seeking; progress via mpv's time-pos
      • Clean shutdown (no lingering audio)
    """

    def __init__(self, master):
        super().__init__(master)

        # ---- Controls bar ----
        bar = ttk.Frame(self); bar.pack(fill="x")
        self.btn_play  = ttk.Button(bar, text="Play",  command=self.play,  state="disabled")
        self.btn_pause = ttk.Button(bar, text="Pause", command=self.pause, state="disabled")
        self.btn_stop  = ttk.Button(bar, text="Stop",  command=self.stop,  state="disabled")
        self.btn_play.pack(side="left")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_stop.pack(side="left", padx=(0,8))

        # Progress (seconds)
        self._in_prog = False
        self._scrub   = False
        self._seek_job = None
        self._was_playing = False
        self.progress = tk.Scale(bar, from_=0, to=1, orient="horizontal", showvalue=0, command=self._on_scale_cmd)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress.bind("<ButtonPress-1>",   self._on_seek_press)
        self.progress.bind("<B1-Motion>",       self._on_seek_drag)
        self.progress.bind("<ButtonRelease-1>", self._on_seek_release)
        self.progress.bind("<Button-1>",        self._on_trough_click, add="+")

        # ---- Video surface ----
        self.surface = tk.Frame(self, bg="black")
        self.surface.pack(fill="both", expand=True)
        self.surface.bind("<Configure>", lambda e: self._set_wid_later())

        # ---- State ----
        self._player = None
        self._first_frame_ready = False
        self._pending_path: str | None = None
        self._duration = 0.0
        self._volume_default = 100
        self._mpv_thread = None
        self._closing = False

        # init mpv
        self._init_mpv()

    # ---------------- mpv init / teardown ----------------
    def _init_mpv(self):
        if mpv is None:
            lab = ttk.Label(self.surface, text=f"python-mpv not available:\n{_mpv_eerr}", foreground="#d33", justify="center")
            lab.place(relx=0.5, rely=0.5, anchor="center")
            return

        # Safer defaults: software decode, no OSC, no border/title, idle
        self._player = mpv.MPV(
            wid=0,                   # set later when widget is realized
            log_handler=None,
            ytdl=False,
            osc=False,
            config=False,
            idle=True,               # stay alive with no file
            hwdec='no',              # avoid D3D11 headaches
            keep_open='no',
            video='auto',
            demuxer_thread='yes',
            audio_client_name='ImageBrowser',
        )

        # Observe properties for UI sync
        @self._player.property_observer('time-pos')
        def _on_time(_, value):
            if value is None or self._scrub or self._in_prog:
                return
            self._in_prog = True
            try:
                self.progress.set(int(value))
            finally:
                self._in_prog = False

        @self._player.property_observer('duration')
        def _on_dur(_, value):
            if value and value > 0:
                self._duration = float(value)
                self._in_prog = True
                try:
                    self.progress.config(from_=0, to=int(self._duration))
                finally:
                    self._in_prog = False

        # First frame displayed → unmute
        @self._player.event_callback('playback-restart')
        def _on_playback_restart(_evt):
            # fired when first frame is shown (and on seeks)
            if not self._first_frame_ready:
                self._first_frame_ready = True
                self.after(0, self._unmute_if_safe)

        self._set_wid_later()

    def _set_wid_later(self):
        # ensure mpv renders into our Tk Frame after resizes
        if not self._player:
            return
        try:
            wid = self.surface.winfo_id()
            self._player.wid = wid
        except Exception:
            pass

    def shutdown(self):
        self._closing = True
        try:
            self.stop()
        except Exception:
            pass
        if self._player:
            try:
                self._player.terminate()  # kill mpv cleanly
            except Exception:
                pass
        self._player = None

    # ---------------- Public API ----------------
    def load(self, path: Path):
        """Select a video: prepare (paused+muted)."""
        if not self._player:
            return
        self.stop()  # reset current
        self._pending_path = str(path)
        self._first_frame_ready = False

        # prepare paused & muted
        try:
            self._player.command('loadfile', self._pending_path, 'replace', 'pause=yes')
            self._player.mute = True
            self._player.volume = self._volume_default
            self._player.time_pos = 0
        except Exception:
            return

        # reset UI
        self._in_prog = True
        try:
            self.progress.config(from_=0, to=1)
            self.progress.set(0)
        finally:
            self._in_prog = False

        self.btn_play.config(state="normal")
        self.btn_pause.config(state="normal")
        self.btn_stop.config(state="normal")

    def play(self):
        if not (self._player and self._pending_path):
            return
        # start paused & muted; mpv will raise playback-restart when it shows first frame
        try:
            self._player.pause = False
        except Exception:
            return

    def pause(self):
        if not self._player: return
        try: self._player.pause = True
        except Exception: pass

    def stop(self):
        if not self._player: return
        try:
            # unload file; returns to idle (silence)
            self._player.command('stop')
            self._player.mute = True
        except Exception:
            pass
        # reset UI
        self._in_prog = True
        try:
            self.progress.set(0)
            self.progress.config(from_=0, to=1)
        finally:
            self._in_prog = False
        self._first_frame_ready = False
        self._pending_path = None

    # ---------------- helpers ----------------
    def _unmute_if_safe(self):
        # Only unmute if a frame was rendered and we're not closing
        if not self._player or self._closing:
            return
        try:
            # If user hasn’t pressed Pause immediately, unmute now
            self._player.mute = False
        except Exception:
            pass

    # ---------------- seeking ----------------
    def _on_scale_cmd(self, val):
        # keyboard nudges only; mouse paths handle themselves
        if not self._player or self._in_prog or self._scrub:
            return
        try:
            self._player.time_pos = float(val)
        except Exception:
            pass

    def _on_seek_press(self, _e):
        if not self._player: return
        self._scrub = True
        self._was_playing = not bool(self._player.pause)
        self.pause()

    def _on_seek_drag(self, e):
        if not self._player: return
        val = self._scale_pixel_to_value(e)
        self._in_prog = True
        try:
            self.progress.set(val)
        finally:
            self._in_prog = False
        # throttle seeks: set a tiny delayed write to mpv
        if self._seek_job:
            try: self.after_cancel(self._seek_job)
            except Exception: pass
        self._seek_job = self.after(80, lambda v=val: self._do_seek(v))

    def _on_seek_release(self, e):
        if not self._player: return
        self._scrub = False
        val = self._scale_pixel_to_value(e)
        self._do_seek(val)
        if self._was_playing:
            self.play()
        self._was_playing = False

    def _on_trough_click(self, e):
        if not self._player: return
        val = self._scale_pixel_to_value(e)
        self._in_prog = True
        try:
            self.progress.set(val)
        finally:
            self._in_prog = False
        self._do_seek(val)
        return "break"

    def _do_seek(self, seconds: float):
        if not self._player: return
        try:
            self._player.time_pos = float(seconds)
        except Exception:
            pass

    def _scale_pixel_to_value(self, event):
        try:
            rng = float(self.progress.cget("to")) - float(self.progress.cget("from"))
            if rng <= 0: return 0
            x = max(0, min(event.x, self.progress.winfo_width()))
            frac = x / max(1, self.progress.winfo_width())
            return int(frac * rng)
        except Exception:
            try: return int(self.progress.get())
            except Exception: return 0
