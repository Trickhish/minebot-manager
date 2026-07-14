"""Live-view window for the mcbot stream server.

Opens a Tkinter window showing a live top-down map streamed from a running
ChunkStreamServer (started by stream_bot.py).  Uses only stdlib for the window
itself; only numpy is needed for rendering.  Works on WSL via WSLg or an X
server (e.g. VcXsrv / X410).

Usage (after stream_bot.py is already running):

    python -m examples.stream_viewer [host] [port] [radius] [interval_ms] [resourcepack]

Defaults:
    host           127.0.0.1
    port           25566
    radius         80        blocks rendered around the bot
    interval_ms    500       display refresh rate
    resourcepack   (none)    path to a .zip or extracted resource pack for
                             real texture colors

Controls:
    Scroll wheel       Zoom in / out
    + / -              Zoom in / out (keyboard)
    Click + drag       Pan
    Double-click       Re-centre view on the bot

Requires numpy:
    source .venv/bin/activate   (project venv already has it)
"""

import base64
import os
import sys
import tkinter as tk
from tkinter import ttk

try:
    import numpy as np  # noqa: F401 – imported here to surface a clear error
except ImportError:
    sys.exit(
        "[viewer] numpy is required for rendering.\n"
        "  Run:  pip install numpy   or   source .venv/bin/activate"
    )

from mcbot.render import encode_png, render_top_down
from mcbot.resourcepack import ResourcePack
from mcbot.stream import ChunkStreamClient


# ---------------------------------------------------------------------------
# Auto-discover a resource pack from standard Minecraft install locations
# ---------------------------------------------------------------------------

def _find_default_resource_pack() -> "str | None":
    """Return the path to a resource pack found in a standard Minecraft
    installation, or None if nothing usable is found.

    Search order (first match wins):
      1. ~/.minecraft/resourcepacks/                                (native Linux)
      2. /mnt/c/Users/*/AppData/Roaming/.minecraft/resourcepacks/  (WSL)

    Inside each resourcepacks folder .zip files are preferred over
    extracted directories; within each type the result is sorted so
    the choice is stable across runs.
    """
    import glob

    def _packs_in(folder: str) -> list:
        if not os.path.isdir(folder):
            return []
        zips = sorted(glob.glob(os.path.join(folder, "*.zip")))
        dirs = sorted(
            p for p in glob.glob(os.path.join(folder, "*/"))
            if os.path.isdir(p)
        )
        return zips + dirs

    # Project root = two levels up from this file (examples/stream_viewer.py)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    candidates = []

    # 1. default_rpack/ / default_rpack.zip sitting in the project root.
    #    Prefer the extracted directory: ResourcePack reads each texture file
    #    individually, so plain file opens are faster than zip seek+decompress.
    for name in ("default_rpack", "default_rpack.zip"):
        p = os.path.join(project_root, name)
        if os.path.exists(p):
            candidates.append(p)

    # 2. Native Linux Minecraft
    candidates.extend(_packs_in(os.path.expanduser("~/.minecraft/resourcepacks")))

    # 3. WSL: Windows-side .minecraft for every Windows user profile found
    mnt_users = "/mnt/c/Users"
    if os.path.isdir(mnt_users):
        for user_dir in sorted(os.listdir(mnt_users)):
            wsl_rp = os.path.join(
                mnt_users, user_dir,
                "AppData", "Roaming", ".minecraft", "resourcepacks",
            )
            candidates.extend(_packs_in(wsl_rp))

    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Zoom table – (numerator, denominator) pairs for PhotoImage.zoom(n).subsample(d)
# 1/4×  1/2×  1×  2×  3×  4×  6×  8×
# ---------------------------------------------------------------------------
_ZOOM_STEPS  = [(1, 4), (1, 2), (1, 1), (2, 1), (3, 1), (4, 1), (6, 1), (8, 1)]
_ZOOM_LABELS = ["¼×",   "½×",   "1×",   "2×",   "3×",   "4×",   "6×",   "8×"]
_DEFAULT_ZOOM_IDX = 2   # 1× by default


def _apply_zoom(photo: tk.PhotoImage, idx: int) -> tk.PhotoImage:
    """Return a new PhotoImage scaled by the zoom step at *idx*."""
    num, den = _ZOOM_STEPS[idx]
    if num > 1:
        photo = photo.zoom(num)
    if den > 1:
        photo = photo.subsample(den)
    return photo


def _png_b64(png_bytes: bytes) -> bytes:
    return base64.b64encode(png_bytes)


# ---------------------------------------------------------------------------
# Main viewer widget
# ---------------------------------------------------------------------------

class LiveMapViewer:
    BG        = "#111118"
    HEADER_BG = "#1e1e2e"
    ACCENT    = "#7c3aed"
    FG        = "#e2e8f0"
    DIM       = "#64748b"
    MARKER    = "#f472b6"

    def __init__(self, root: tk.Tk, client: ChunkStreamClient,
                 radius: int, interval_ms: int,
                 resource_pack: "ResourcePack | None"):
        self.root         = root
        self.client       = client
        self.radius       = radius
        self.interval_ms  = interval_ms
        self.resource_pack = resource_pack

        self._zoom_idx   = _DEFAULT_ZOOM_IDX
        self._pan_x      = 0   # pixel offset from canvas centre
        self._pan_y      = 0
        self._drag_start = None
        self._photo_ref  = None  # keep PhotoImage alive

        self._frame_count = 0
        self._last_render: "bytes | None" = None   # last PNG bytes (for re-zoom without re-render)

        root.title("⛏  MCbot – Live Map")
        root.configure(bg=self.BG)
        root.minsize(400, 400)

        self._build_ui()
        self._bind_events()
        self._tick()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── header ──────────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=self.HEADER_BG, pady=0)
        header.pack(side=tk.TOP, fill=tk.X)

        # left: title
        tk.Label(
            header, text="⛏  MCbot Live Map",
            bg=self.HEADER_BG, fg="white",
            font=("Helvetica", 13, "bold"), padx=12, pady=8,
        ).pack(side=tk.LEFT)

        # right: status
        self._status_var = tk.StringVar(value="connecting…")
        tk.Label(
            header, textvariable=self._status_var,
            bg=self.HEADER_BG, fg="#a78bfa",
            font=("Helvetica", 10), padx=12,
        ).pack(side=tk.RIGHT)

        # ── toolbar ─────────────────────────────────────────────────────
        toolbar = tk.Frame(self.root, bg=self.HEADER_BG, padx=8, pady=4)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Label(toolbar, text="Zoom:", bg=self.HEADER_BG, fg=self.DIM,
                 font=("Helvetica", 9)).pack(side=tk.LEFT)

        btn_style = dict(bg="#2d2d44", fg=self.FG, relief=tk.FLAT,
                         activebackground=self.ACCENT, activeforeground="white",
                         font=("Helvetica", 9, "bold"), padx=6, pady=2, cursor="hand2")

        tk.Button(toolbar, text="−", command=self._zoom_out, **btn_style).pack(side=tk.LEFT, padx=(4, 0))

        self._zoom_label_var = tk.StringVar(value=_ZOOM_LABELS[_DEFAULT_ZOOM_IDX])
        tk.Label(toolbar, textvariable=self._zoom_label_var,
                 bg=self.HEADER_BG, fg=self.FG,
                 font=("Courier", 10, "bold"), width=4).pack(side=tk.LEFT)

        tk.Button(toolbar, text="+", command=self._zoom_in, **btn_style).pack(side=tk.LEFT)

        tk.Button(toolbar, text="⌖ Centre", command=self._recenter,
                  **btn_style).pack(side=tk.LEFT, padx=(12, 0))

        if self.resource_pack:
            tk.Label(toolbar, text="🎨 texture pack active",
                     bg=self.HEADER_BG, fg="#34d399",
                     font=("Helvetica", 9), padx=12).pack(side=tk.RIGHT)

        # ── canvas ──────────────────────────────────────────────────────
        self._canvas = tk.Canvas(
            self.root, bg=self.BG, highlightthickness=0,
            cursor="fleur",
        )
        self._canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=0, pady=0)

        # ── footer ──────────────────────────────────────────────────────
        footer = tk.Frame(self.root, bg=self.HEADER_BG, pady=3)
        footer.pack(side=tk.BOTTOM, fill=tk.X)

        self._coords_var = tk.StringVar(value="pos: —")
        tk.Label(footer, textvariable=self._coords_var,
                 bg=self.HEADER_BG, fg=self.FG,
                 font=("Courier", 9), padx=12).pack(side=tk.LEFT)

        pack_text = f"radius={self.radius}  interval={self.interval_ms}ms"
        tk.Label(footer, text=pack_text,
                 bg=self.HEADER_BG, fg=self.DIM,
                 font=("Courier", 9), padx=12).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Event bindings
    # ------------------------------------------------------------------

    def _bind_events(self):
        c = self._canvas

        # zoom – mouse wheel (Linux Button-4/5, Windows/WSLg MouseWheel)
        c.bind("<Button-4>",    lambda e: self._zoom_in())
        c.bind("<Button-5>",    lambda e: self._zoom_out())
        c.bind("<MouseWheel>",  lambda e: self._zoom_in() if e.delta > 0 else self._zoom_out())

        # zoom – keyboard
        self.root.bind("+", lambda e: self._zoom_in())
        self.root.bind("=", lambda e: self._zoom_in())   # + without shift
        self.root.bind("-", lambda e: self._zoom_out())

        # pan
        c.bind("<ButtonPress-1>",   self._drag_start)
        c.bind("<B1-Motion>",       self._drag_motion)
        c.bind("<Double-Button-1>", lambda e: self._recenter())

    # ------------------------------------------------------------------
    # Zoom & pan helpers
    # ------------------------------------------------------------------

    def _zoom_in(self):
        if self._zoom_idx < len(_ZOOM_STEPS) - 1:
            self._zoom_idx += 1
            self._zoom_label_var.set(_ZOOM_LABELS[self._zoom_idx])
            self._redisplay()

    def _zoom_out(self):
        if self._zoom_idx > 0:
            self._zoom_idx -= 1
            self._zoom_label_var.set(_ZOOM_LABELS[self._zoom_idx])
            self._redisplay()

    def _recenter(self):
        self._pan_x = 0
        self._pan_y = 0
        self._redisplay()

    def _drag_start(self, event):
        self._drag_start_pos = (event.x, event.y)
        self._drag_start_pan = (self._pan_x, self._pan_y)

    def _drag_motion(self, event):
        if self._drag_start_pos is None:
            return
        sx, sy = self._drag_start_pos
        px, py = self._drag_start_pan
        self._pan_x = px + (event.x - sx)
        self._pan_y = py + (event.y - sy)
        self._redisplay()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _tick(self):
        """Called every interval_ms to re-render the map."""
        try:
            self._render_frame()
        except Exception as exc:
            self._status_var.set(f"render error: {exc}")
        self.root.after(self.interval_ms, self._tick)

    def _render_frame(self):
        pos     = self.client.get_position()
        n_chunks = len(self.client.world.chunks)

        img = render_top_down(
            self.client.world,
            int(pos["x"]), int(pos["z"]),
            self.radius,
            bot_position=(pos["x"], pos["y"], pos["z"]),
            resource_pack=self.resource_pack,
        )
        self._last_render = encode_png(img)
        self._frame_count += 1

        self._status_var.set(
            f"chunks={n_chunks}  frame={self._frame_count}"
        )
        self._coords_var.set(
            f"pos: ({pos['x']:.1f}, {pos['y']:.1f}, {pos['z']:.1f})"
        )
        self._redisplay()

    def _redisplay(self):
        """Push the last-rendered PNG onto the canvas at current zoom & pan."""
        if self._last_render is None:
            return

        photo = tk.PhotoImage(data=_png_b64(self._last_render))
        photo = _apply_zoom(photo, self._zoom_idx)

        self._photo_ref = photo  # prevent GC

        w = self._canvas.winfo_width()
        h = self._canvas.winfo_height()
        cx = w // 2 + self._pan_x
        cy = h // 2 + self._pan_y

        self._canvas.delete("all")
        self._canvas.create_image(cx, cy, image=photo)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    host        = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port        = int(sys.argv[2]) if len(sys.argv) > 2 else 25566
    radius      = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    interval_ms = int(sys.argv[4]) if len(sys.argv) > 4 else 500
    pack_path   = sys.argv[5] if len(sys.argv) > 5 else None

    resource_pack = None
    if pack_path:
        resource_pack = ResourcePack(pack_path)
        print(f"[viewer] using resource pack: {pack_path}")
    else:
        auto = _find_default_resource_pack()
        if auto:
            resource_pack = ResourcePack(auto)
            print(f"[viewer] auto-detected resource pack: {auto}")
        else:
            print("[viewer] no resource pack found — using approximate colours")
            print("         (add a pack to ~/.minecraft/resourcepacks/ or")
            print("          pass its path as the 5th argument)")

    print(f"[viewer] connecting to {host}:{port} …")
    try:
        client = ChunkStreamClient(host, port)
    except ConnectionRefusedError:
        sys.exit(
            f"[viewer] connection refused at {host}:{port}\n"
            "  Make sure stream_bot.py is running and has spawned."
        )

    print(f"[viewer] connected  (world version {client.world.block_table.version})")
    print(f"[viewer] rendering every {interval_ms}ms  radius={radius}")
    if resource_pack:
        print("[viewer] texture colours from resource pack")

    root = tk.Tk()

    # Start with a sensible default window size (radius × 2 pixels + chrome)
    map_px = radius * 2 + 1
    win_w  = min(map_px + 20, 1000)
    win_h  = min(map_px + 90, 900)
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{win_w}x{win_h}+{(sw - win_w) // 2}+{(sh - win_h) // 2}")

    viewer = LiveMapViewer(root, client, radius, interval_ms, resource_pack)

    def _on_close():
        client.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
