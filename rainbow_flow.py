"""
RAINBOW FLOW – LED Floor Animation
Row-wise rainbow wave that flows across the 10x4 floor.

Layout (same as MinesFrame in unified_led_floor_system.py):
    Row 0  ->  tiles  1- 4
    Row 1  ->  tiles  5- 8
    ...
    Row 9  ->  tiles 37-40

How it works:
  - Each row gets a hue derived from a scrolling offset.
  - offset advances by `speed` every frame -> colours flow downward.
  - All 4 tiles in a row share the same colour.
  - The virtual grid mirrors the physical floor in real-time.
  - Animation STARTS AUTOMATICALLY on launch.
  - START / STOP buttons are in a top toolbar -- always visible.
"""

import json
import socket
import tkinter as tk
import threading
import time
import logging

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        logging.FileHandler('rainbow_flow.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('RainbowFlow')

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────────────────────
CONFIG_FILE = r'D:\learning material\college\mechatron\WORKING5\esp_config.json'
ESP_PORT    = 8080

# Physical floor layout -- Row 0 = tiles 1-4, Row 9 = tiles 37-40
GRID_LAYOUT = [list(range(r * 4 + 1, r * 4 + 5)) for r in range(10)]
NUM_ROWS    = 10
NUM_COLS    = 4
TOTAL_TILES = NUM_ROWS * NUM_COLS


# ──────────────────────────────────────────────────────────────────────────────
#  COLOUR HELPER
# ──────────────────────────────────────────────────────────────────────────────
def hsv_to_rgb(h, s=1.0, v=1.0):
    """h in [0,1), s/v in [0,1].  Returns (r, g, b) each 0-255."""
    h = h % 1.0
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    i = i % 6
    if   i == 0: r, g, b = v, t, p
    elif i == 1: r, g, b = q, v, p
    elif i == 2: r, g, b = p, v, t
    elif i == 3: r, g, b = p, q, v
    elif i == 4: r, g, b = t, p, v
    else:        r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


def rgb_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"


# ──────────────────────────────────────────────────────────────────────────────
#  CONNECTION MANAGER
# ──────────────────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self, config_file):
        self.socket_lock      = threading.Lock()
        self.esp_sockets      = {}
        self.esp_status       = {}
        self.tile_to_esp      = {}
        self.esp_map          = {}
        self.reconnect_active = True
        self.on_status_change = None   # wired to GUI after root window exists

        self._load_config(config_file)
        threading.Thread(target=self._initial_connect, daemon=True).start()
        threading.Thread(target=self._reconnect_loop,  daemon=True).start()

    def _load_config(self, path):
        with open(path, 'r') as f:
            cfg = json.load(f)
        self.esp_map = cfg['esps']
        for mac, tiles in self.esp_map.items():
            self.esp_status[mac]  = 'offline'
            self.esp_sockets[mac] = None
            for idx, tile_num in enumerate(tiles):
                self.tile_to_esp[tile_num] = (mac, idx)
        logger.info(f"Config: {len(self.esp_map)} ESPs, {TOTAL_TILES} tiles")

    def _connect(self, mac):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((f"esp-{mac}.local", ESP_PORT))
            self.esp_status[mac] = 'online'
            logger.info(f"Connected: {mac}")
            self._fire()
            return s
        except Exception as e:
            self.esp_status[mac] = 'offline'
            logger.warning(f"Failed {mac}: {e}")
            return None

    def _initial_connect(self):
        time.sleep(0.8)
        for mac in self.esp_map:
            with self.socket_lock:
                self.esp_sockets[mac] = self._connect(mac)
            time.sleep(0.25)
        self._fire()

    def _reconnect_loop(self):
        while self.reconnect_active:
            time.sleep(5)
            for mac in list(self.esp_map.keys()):
                if self.esp_status.get(mac) == 'online':
                    continue
                sock = self._connect(mac)
                if sock:
                    with self.socket_lock:
                        self.esp_sockets[mac] = sock

    def send_color(self, tile_num, r, g, b):
        if tile_num not in self.tile_to_esp:
            return
        mac, local_idx = self.tile_to_esp[tile_num]
        cmd = f"T{local_idx + 1}_COLOR_{r}_{g}_{b}\n"
        with self.socket_lock:
            if not self.esp_sockets.get(mac):
                return
            try:
                self.esp_sockets[mac].sendall(cmd.encode())
            except Exception as e:
                logger.error(f"Send error {mac}: {e}")
                try:   self.esp_sockets[mac].close()
                except Exception: pass
                self.esp_sockets[mac] = None
                self.esp_status[mac]  = 'offline'
                self._fire()

    def send_off(self, tile_num):
        if tile_num not in self.tile_to_esp:
            return
        mac, local_idx = self.tile_to_esp[tile_num]
        with self.socket_lock:
            if not self.esp_sockets.get(mac):
                return
            try:
                self.esp_sockets[mac].sendall(
                    f"T{local_idx + 1}_OFF\n".encode())
            except Exception:
                self.esp_sockets[mac] = None
                self.esp_status[mac]  = 'offline'

    def clear_all(self):
        for t in range(1, TOTAL_TILES + 1):
            self.send_off(t)

    def shutdown(self):
        self.reconnect_active = False
        with self.socket_lock:
            for s in self.esp_sockets.values():
                if s:
                    try: s.close()
                    except Exception: pass

    def _fire(self):
        if self.on_status_change:
            try: self.on_status_change()
            except Exception: pass


# ──────────────────────────────────────────────────────────────────────────────
#  RAINBOW FLOW APP
# ──────────────────────────────────────────────────────────────────────────────
class RainbowFlowApp:
    def __init__(self):
        self.manager     = ConnectionManager(CONFIG_FILE)
        self.running     = False
        self._thread     = None
        self.tile_labels = {}

        # tk.DoubleVar placeholders -- assigned inside _build_gui() AFTER tk.Tk()
        self._speed      = None
        self._brightness = None
        self._delay      = None

        self._build_gui()

        # Wire ESP status updates into GUI (thread-safe via after())
        self.manager.on_status_change = lambda: self.root.after(
            0, self._refresh_esp_panel)

        # Auto-start animation 600 ms after window appears
        self.root.after(600, self._start)

    # ──────────────────────────────────────────────────────────────────────────
    #  GUI
    # ──────────────────────────────────────────────────────────────────────────
    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("Rainbow Flow -- LED Floor")
        self.root.geometry("1200x820")
        self.root.configure(bg='#111111')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── DoubleVars MUST live after tk.Tk() ────────────────────────────────
        self._speed      = tk.DoubleVar(master=self.root, value=0.015)
        self._brightness = tk.DoubleVar(master=self.root, value=1.0)
        self._delay      = tk.DoubleVar(master=self.root, value=0.05)

        outer = tk.Frame(self.root, bg='#111111')
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # RIGHT -- ESP status panel (fixed 260 px)
        right = tk.Frame(outer, bg='#222222', relief=tk.RAISED, bd=2, width=260)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        right.pack_propagate(False)
        self._build_esp_panel(right)

        # LEFT -- toolbar + grid + sliders
        left = tk.Frame(outer, bg='#111111')
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_toolbar(left)   # <-- START/STOP always at top
        self._build_grid(left)
        self._build_sliders(left)

        self.root.after(2000, self._refresh_esp_panel)

    # ── Top toolbar  (title only – buttons live in the right ESP panel) ────────
    def _build_toolbar(self, parent):
        bar = tk.Frame(parent, bg='#1a1a1a', relief=tk.RAISED, bd=2)
        bar.pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            bar,
            text="🌈  RAINBOW FLOW",
            bg='#1a1a1a', fg='#ff44ff',
            font=('Arial', 20, 'bold')
        ).pack(side=tk.LEFT, padx=14, pady=10)

        tk.Label(
            bar,
            text="Row-wise colour wave  |  10 rows × 4 columns",
            bg='#1a1a1a', fg='#888888',
            font=('Arial', 10)
        ).pack(side=tk.LEFT, padx=(0, 14), pady=10)

    # ── Tile grid ────────────────────────────────────────────────────────────
    def _build_grid(self, parent):
        gf = tk.LabelFrame(
            parent,
            text="Virtual Floor  (Row 0 = tiles 1-4  to  Row 9 = tiles 37-40)",
            bg='#222222', fg='#00ff88',
            font=('Arial', 10, 'bold'), padx=8, pady=6
        )
        gf.pack(fill=tk.BOTH, expand=True)

        tk.Label(gf, text="", bg='#222222', width=4).grid(row=0, column=0)
        for c in range(NUM_COLS):
            tk.Label(
                gf, text=f"Col {c+1}",
                bg='#222222', fg='#666666',
                font=('Arial', 8)
            ).grid(row=0, column=c + 1, padx=4)

        for row_idx, tile_row in enumerate(GRID_LAYOUT):
            tk.Label(
                gf, text=f"R{row_idx}",
                bg='#222222', fg='#555555',
                font=('Arial', 8), width=4, anchor='e'
            ).grid(row=row_idx + 1, column=0, padx=(0, 4))

            for col_idx, tile_num in enumerate(tile_row):
                lbl = tk.Label(
                    gf,
                    text=str(tile_num),
                    width=8, height=2,
                    bg='#1a1a1a', fg='#444444',
                    font=('Arial', 11, 'bold'),
                    relief=tk.RAISED, bd=3
                )
                lbl.grid(row=row_idx + 1, column=col_idx + 1,
                         padx=4, pady=2, sticky='nsew')
                self.tile_labels[tile_num] = lbl

        for c in range(1, NUM_COLS + 1):
            gf.columnconfigure(c, weight=1)
        for r in range(1, NUM_ROWS + 1):
            gf.rowconfigure(r, weight=1)

    # ── Sliders ──────────────────────────────────────────────────────────────
    def _build_sliders(self, parent):
        sf = tk.Frame(parent, bg='#1a1a1a', relief=tk.SUNKEN, bd=1)
        sf.pack(fill=tk.X, pady=(6, 0))

        self._add_slider(sf, "Wave Speed",  self._speed,      0.001, 0.08, 0,
                         "<-- slower   faster -->")
        self._add_slider(sf, "Brightness",  self._brightness, 0.1,   1.0,  1,
                         "<-- dimmer   brighter -->")
        self._add_slider(sf, "Frame Delay", self._delay,      0.02,  0.30, 2,
                         "<-- faster   slower -->")

    def _add_slider(self, parent, label, var, from_, to, row, hint):
        tk.Label(
            parent, text=label,
            bg='#1a1a1a', fg='#cccccc',
            font=('Arial', 9, 'bold')
        ).grid(row=row, column=0, sticky='e', padx=(10, 6), pady=4)

        tk.Scale(
            parent,
            variable=var, from_=from_, to=to,
            orient=tk.HORIZONTAL, resolution=0.001,
            bg='#222222', fg='white', troughcolor='#444444',
            highlightthickness=0, length=300
        ).grid(row=row, column=1, sticky='ew', pady=4)

        tk.Label(
            parent, text=hint,
            bg='#1a1a1a', fg='#555555',
            font=('Arial', 8)
        ).grid(row=row, column=2, sticky='w', padx=(8, 10))

        parent.columnconfigure(1, weight=1)

    # ── ESP status panel ─────────────────────────────────────────────────────
    def _build_esp_panel(self, parent):

        # ── START / STOP buttons at the very top of the right panel ──────────
        btn_frame = tk.Frame(parent, bg='#222222')
        btn_frame.pack(fill=tk.X, padx=10, pady=(14, 6))

        self.start_btn = tk.Button(
            btn_frame,
            text="▶  START",
            command=self._start,
            bg='#007700', fg='white',
            font=('Arial', 13, 'bold'),
            height=2, relief=tk.RAISED, bd=3
        )
        self.start_btn.pack(fill=tk.X, pady=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame,
            text="■  STOP",
            command=self._stop,
            bg='#aa0000', fg='white',
            font=('Arial', 13, 'bold'),
            height=2, relief=tk.RAISED, bd=3,
            state=tk.DISABLED
        )
        self.stop_btn.pack(fill=tk.X)

        tk.Frame(parent, bg='#444444', height=2).pack(fill=tk.X, padx=8, pady=(10, 4))

        tk.Label(
            parent, text="ESP32 STATUS",
            bg='#222222', fg='#00ff88',
            font=('Arial', 13, 'bold')
        ).pack(pady=(4, 2))

        self._esp_count_label = tk.Label(
            parent, text="0 / 0 online",
            bg='#222222', fg='white',
            font=('Arial', 10)
        )
        self._esp_count_label.pack(pady=(0, 6))

        lf = tk.LabelFrame(
            parent, text="Controllers",
            bg='#222222', fg='white',
            font=('Arial', 9, 'bold'), padx=4, pady=4
        )
        lf.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        canvas = tk.Canvas(lf, bg='#222222', highlightthickness=0)
        sb     = tk.Scrollbar(lf, orient=tk.VERTICAL, command=canvas.yview)
        self._esp_rows_frame = tk.Frame(canvas, bg='#222222')

        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.create_window((0, 0), window=self._esp_rows_frame, anchor='nw')
        self._esp_rows_frame.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
        )

        self._reconnect_lbl = tk.Label(
            parent, text="Auto-reconnect: active",
            bg='#222222', fg='#666666',
            font=('Arial', 8)
        )
        self._reconnect_lbl.pack(pady=(6, 2))

        tk.Button(
            parent, text="Reconnect All",
            command=self._manual_reconnect,
            bg='#3a7abf', fg='white',
            font=('Arial', 9, 'bold')
        ).pack(padx=8, pady=(0, 12), fill=tk.X)

    def _refresh_esp_panel(self):
        for w in self._esp_rows_frame.winfo_children():
            w.destroy()

        online = 0
        for mac, tiles in self.manager.esp_map.items():
            status = self.manager.esp_status.get(mac, 'offline')
            if status == 'online':
                online += 1

            row = tk.Frame(self._esp_rows_frame, bg='#333333',
                           relief=tk.RAISED, bd=1)
            row.pack(fill=tk.X, pady=2, padx=2)

            tk.Label(row, text=mac[-6:], bg='#333333', fg='white',
                     font=('Arial', 9, 'bold'), width=8, anchor='w'
            ).pack(side=tk.LEFT, padx=4)

            tk.Label(
                row,
                text="ON" if status == 'online' else "OFF",
                bg='#5cb85c' if status == 'online' else '#d9534f',
                fg='white', font=('Arial', 8, 'bold'), width=4
            ).pack(side=tk.LEFT, padx=4)

            tk.Label(row, text=f"Tiles: {tiles}",
                     bg='#333333', fg='#bbbbbb',
                     font=('Arial', 7), anchor='w'
            ).pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)

        total = len(self.manager.esp_map)
        clr   = '#00ff00' if online == total else '#ffaa00' if online > 0 else '#ff4444'
        self._esp_count_label.config(text=f"{online} / {total} online", fg=clr)

        offline = [m for m, s in self.manager.esp_status.items() if s == 'offline']
        if offline:
            self._reconnect_lbl.config(
                text=f"Reconnecting: {', '.join(m[-6:] for m in offline[:2])}...",
                fg='#ffaa00')
        else:
            self._reconnect_lbl.config(text="All online", fg='#00aa00')

        self.root.after(3000, self._refresh_esp_panel)

    def _manual_reconnect(self):
        def _do():
            with self.manager.socket_lock:
                for s in self.manager.esp_sockets.values():
                    if s:
                        try: s.close()
                        except Exception: pass
                for mac in self.manager.esp_map:
                    self.manager.esp_sockets[mac] = None
                    self.manager.esp_status[mac]  = 'offline'
            for mac in self.manager.esp_map:
                sock = self.manager._connect(mac)
                with self.manager.socket_lock:
                    self.manager.esp_sockets[mac] = sock
                time.sleep(0.25)
            self.manager._fire()
        threading.Thread(target=_do, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    #  ANIMATION
    # ──────────────────────────────────────────────────────────────────────────
    def _start(self):
        if self.running:
            return
        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self._thread = threading.Thread(target=self._animation_loop, daemon=True)
        self._thread.start()
        logger.info("Rainbow flow started")

    def _stop(self):
        self.running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        if self._thread:
            self._thread.join(timeout=2)
        self.manager.clear_all()
        for lbl in self.tile_labels.values():
            lbl.config(bg='#1a1a1a', fg='#444444')
        logger.info("Rainbow flow stopped")

    def _animation_loop(self):
        """
        Row r  ->  hue = (offset + r / NUM_ROWS) % 1.0
        All 4 tiles in the row get that colour.
        offset advances by `speed` each frame -> rainbow scrolls downward.
        """
        offset = 0.0

        while self.running:
            speed      = self._speed.get()
            brightness = self._brightness.get()
            delay      = self._delay.get()
            txt_color  = '#000000' if brightness > 0.5 else '#ffffff'

            for row_idx, tile_row in enumerate(GRID_LAYOUT):
                if not self.running:
                    break

                hue       = (offset + row_idx / NUM_ROWS) % 1.0
                r, g, b   = hsv_to_rgb(hue, 1.0, brightness)
                hex_color = rgb_hex(r, g, b)

                for tile_num in tile_row:
                    # Send to physical floor
                    self.manager.send_color(tile_num, r, g, b)
                    # Update virtual grid safely from background thread
                    self.root.after(
                        0,
                        lambda lbl=self.tile_labels[tile_num],
                               c=hex_color, t=txt_color:
                            lbl.config(bg=c, fg=t)
                    )

            offset = (offset + speed) % 1.0
            time.sleep(delay)

    # ──────────────────────────────────────────────────────────────────────────
    #  LIFECYCLE
    # ──────────────────────────────────────────────────────────────────────────
    def _on_close(self):
        self.running = False
        self.manager.clear_all()
        self.manager.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ──────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  RAINBOW FLOW -- LED Floor Animation")
    print("  Row-wise hue wave  |  10 rows x 4 columns")
    print("  Animation starts automatically on launch.")
    print("=" * 55)

    try:
        app = RainbowFlowApp()
        app.run()
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        print("Make sure esp_config.json exists at the configured path.")
        input("Press Enter to exit...")
