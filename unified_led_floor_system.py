"""
UNIFIED LED FLOOR SYSTEM
Merges Memory Path Game and Mines Game into a single application.

Architecture:
  - FloorConnectionManager  : Singleton-style shared TCP connection manager
  - UnifiedApp              : Root Tkinter window, main menu, persistent ESP panel
  - MemoryPathFrame         : Memory Path game encapsulated in a Frame
  - MinesFrame              : Mines game encapsulated in a Frame
"""

import json
import socket
import tkinter as tk
from tkinter import messagebox
import threading
import time
import random
import logging
import os

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING  – single file for the whole system
# ─────────────────────────────────────────────────────────────────────────────
def _setup_root_logger():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if root_logger.handlers:
        return  # already configured
    fmt = logging.Formatter('%(asctime)s [%(name)s] %(message)s')
    fh = logging.FileHandler('floor_system.log')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root_logger.addHandler(fh)
    root_logger.addHandler(sh)

_setup_root_logger()


# ─────────────────────────────────────────────────────────────────────────────
#  FLOOR CONNECTION MANAGER  (singleton-style, lives for the whole app lifetime)
# ─────────────────────────────────────────────────────────────────────────────
class FloorConnectionManager:
    """
    Manages all 10 TCP connections to ESP32 controllers.
    Provides thread-safe send_color(), clear_all(), and auto-reconnect with
    state restoration.  Never closed when switching between games.
    """

    CONFIG_FILE = r'D:\learning material\college\mechatron\WORKING5\esp_config.json'
    ESP_PORT    = 8080
    RECONNECT_INTERVAL = 5  # seconds

    def __init__(self):
        self.logger = logging.getLogger('ConnectionManager')

        self.esp_sockets: dict = {}   # mac -> socket | None
        self.esp_status:  dict = {}   # mac -> 'online' | 'offline'
        self.socket_lock  = threading.Lock()

        # tile_num -> (r,g,b) | None  –  persists across game switches
        self.tile_states: dict = {}

        self.reconnect_active = True

        # GUI callback – set by UnifiedApp so the panel always reflects reality
        self._on_status_change = None   # callable()

        self._load_config()
        self._start_background_threads()

    # ── Config ───────────────────────────────────────────────────────────────
    def _load_config(self):
        try:
            with open(self.CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Config not found: {self.CONFIG_FILE}")
            raise
        except Exception as e:
            self.logger.error(f"Config load error: {e}")
            raise

        self.grid_rows   = cfg['grid']['rows']
        self.grid_cols   = cfg['grid']['cols']
        self.esp_map     = cfg['esps']          # mac -> [tile, tile, ...]
        self.total_tiles = self.grid_rows * self.grid_cols

        self.tile_to_esp: dict = {}
        for mac, tiles in self.esp_map.items():
            for idx, tile_num in enumerate(tiles):
                self.tile_to_esp[tile_num] = (mac, idx)
            self.esp_status[mac]  = 'offline'
            self.esp_sockets[mac] = None

        self.logger.info(
            f"Config loaded: {self.grid_rows}x{self.grid_cols} grid, "
            f"{len(self.esp_map)} ESPs, {self.total_tiles} tiles"
        )

    # ── Background threads ────────────────────────────────────────────────────
    def _start_background_threads(self):
        threading.Thread(target=self._initial_connect, daemon=True).start()
        threading.Thread(target=self._reconnect_loop,  daemon=True).start()

    def _initial_connect(self):
        self.logger.info("Initial ESP connection sweep...")
        time.sleep(1)
        for mac in self.esp_map.keys():
            sock = self._connect(mac)
            with self.socket_lock:
                self.esp_sockets[mac] = sock
            time.sleep(0.3)
        online = sum(1 for s in self.esp_status.values() if s == 'online')
        self.logger.info(f"Initial connect done: {online}/{len(self.esp_map)} online")
        self._fire_status_change()

    def _reconnect_loop(self):
        while self.reconnect_active:
            time.sleep(self.RECONNECT_INTERVAL)
            for mac in list(self.esp_map.keys()):
                if self.esp_status.get(mac) == 'online':
                    continue
                self.logger.info(f"Auto-reconnect attempt: {mac}")
                sock = self._connect(mac)
                if sock:
                    with self.socket_lock:
                        self.esp_sockets[mac] = sock
                    self._restore_tiles_for_esp(mac)
                    self._fire_status_change()

    # ── Low-level connect ─────────────────────────────────────────────────────
    def _connect(self, mac) -> socket.socket | None:
        try:
            hostname = f"esp-{mac}.local"
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((hostname, self.ESP_PORT))
            self.esp_status[mac] = 'online'
            self.logger.info(f"Connected: {mac} ({hostname})")
            return s
        except Exception as e:
            self.esp_status[mac] = 'offline'
            self.logger.warning(f"Failed connect {mac}: {e}")
            return None

    # ── Tile restoration after reconnect ─────────────────────────────────────
    def _restore_tiles_for_esp(self, mac):
        for tile_num in self.esp_map.get(mac, []):
            color = self.tile_states.get(tile_num)
            if color and color != (0, 0, 0):
                cmd = f"COLOR_{color[0]}_{color[1]}_{color[2]}"
            else:
                cmd = "OFF"
            self._raw_send_cmd(tile_num, cmd)
            time.sleep(0.04)

    # ── Public API ────────────────────────────────────────────────────────────
    def send_color(self, tile_num: int, r: int, g: int, b: int) -> bool:
        """
        Unified send method used by both games.
        Updates tile_states so reconnect can restore them.
        """
        if tile_num not in self.tile_to_esp:
            return False

        color = (r, g, b)
        self.tile_states[tile_num] = None if color == (0, 0, 0) else color

        if color == (0, 0, 0):
            return self._raw_send_cmd(tile_num, "OFF")
        else:
            return self._raw_send_cmd(tile_num, f"COLOR_{r}_{g}_{b}")

    def clear_all(self):
        """Send OFF to every tile and clear tile_states."""
        self.logger.info("CLEAR ALL tiles")
        for tile_num in range(1, self.total_tiles + 1):
            self.tile_states[tile_num] = None
            self._raw_send_cmd(tile_num, "OFF")

    def reconnect_all_manual(self):
        """Close every socket and reconnect from scratch."""
        def _do():
            self.logger.info("Manual reconnect all ESPs...")
            with self.socket_lock:
                for s in self.esp_sockets.values():
                    if s:
                        try:
                            s.close()
                        except Exception:
                            pass
                self.esp_sockets.clear()
                for mac in self.esp_map.keys():
                    self.esp_sockets[mac] = None
                    self.esp_status[mac]  = 'offline'

            for mac in self.esp_map.keys():
                sock = self._connect(mac)
                with self.socket_lock:
                    self.esp_sockets[mac] = sock
                time.sleep(0.3)

            self._fire_status_change()

        threading.Thread(target=_do, daemon=True).start()

    # ── Internal raw send ─────────────────────────────────────────────────────
    def _raw_send_cmd(self, tile_num: int, command: str) -> bool:
        if tile_num not in self.tile_to_esp:
            return False
        mac, local_idx = self.tile_to_esp[tile_num]

        with self.socket_lock:
            # Lazy-connect if socket is None
            if self.esp_sockets.get(mac) is None:
                self.esp_sockets[mac] = self._connect(mac)

            sock = self.esp_sockets.get(mac)
            if not sock:
                return False

            try:
                sock.sendall(f"T{local_idx + 1}_{command}\n".encode())
                return True
            except Exception as e:
                self.logger.error(f"Send error {mac} tile {tile_num}: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                self.esp_sockets[mac] = None
                self.esp_status[mac]  = 'offline'
                self._fire_status_change()
                return False

    # ── GUI callback ──────────────────────────────────────────────────────────
    def _fire_status_change(self):
        if self._on_status_change:
            try:
                self._on_status_change()
            except Exception:
                pass

    def shutdown(self):
        self.reconnect_active = False
        with self.socket_lock:
            for s in self.esp_sockets.values():
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass


# ─────────────────────────────────────────────────────────────────────────────
#  MEMORY PATH FRAME
# ─────────────────────────────────────────────────────────────────────────────
class MemoryPathFrame(tk.Frame):
    """
    Memory Path Game UI encapsulated in a Frame.
    Tile layout: Row 0 = tiles 37-40  (top),  Row 9 = tiles 1-4 (bottom).
    Player 1 uses columns 0-1,  Player 2 uses columns 2-3.
    """

    GRID_LAYOUT = [
        [37, 38, 39, 40],  # Row 0 – TOP
        [33, 34, 35, 36],
        [29, 30, 31, 32],
        [25, 26, 27, 28],
        [21, 22, 23, 24],
        [17, 18, 19, 20],
        [13, 14, 15, 16],
        [ 9, 10, 11, 12],
        [ 5,  6,  7,  8],
        [ 1,  2,  3,  4],  # Row 9 – BOTTOM
    ]

    COLOR_GREEN = (0, 255, 0)
    COLOR_RED   = (255, 0, 0)
    COLOR_BLACK = (0, 0, 0)

    def __init__(self, parent, manager: FloorConnectionManager, back_callback):
        super().__init__(parent, bg='#1e1e1e')
        self.manager      = manager
        self.back_cb      = back_callback
        self.logger       = logging.getLogger('MemoryPath')

        self.game_active    = False
        self.preview_running = False
        self._stop_preview   = False

        self.p1_path = []
        self.p2_path = []
        self.p1_step = 0
        self.p2_step = 0

        self.tile_labels: dict = {}

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Title row
        title_bar = tk.Frame(self, bg='#1e1e1e')
        title_bar.pack(fill=tk.X, padx=10, pady=(10, 4))

        tk.Button(
            title_bar, text="◀  Back to Menu",
            command=self._back_to_menu,
            bg='#555555', fg='white',
            font=('Arial', 10, 'bold')
        ).pack(side=tk.LEFT)

        tk.Label(
            title_bar, text="MEMORY PATH GAME",
            bg='#1e1e1e', fg='#00ff00',
            font=('Arial', 24, 'bold')
        ).pack(side=tk.LEFT, padx=20)

        # Content row: grid | controls
        content = tk.Frame(self, bg='#1e1e1e')
        content.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        grid_col = tk.Frame(content, bg='#1e1e1e')
        grid_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        ctrl_col = tk.Frame(content, bg='#2e2e2e', relief=tk.RAISED, bd=2)
        ctrl_col.pack(side=tk.LEFT, fill=tk.Y)

        self._build_grid(grid_col)
        self._build_controls(ctrl_col)

        # Status bar
        self.status_label = tk.Label(
            self,
            text="Ready – press Full Reset to start",
            bg='#2e2e2e', fg='white',
            font=('Arial', 11), anchor='w', padx=10, pady=5
        )
        self.status_label.pack(fill=tk.X, padx=10, pady=(4, 8))

    def _build_grid(self, parent):
        gf = tk.LabelFrame(
            parent,
            text="LED Floor Grid  (10 rows × 4 columns  |  Top to Bottom)",
            bg='#2e2e2e', fg='white',
            font=('Arial', 12, 'bold'), padx=10, pady=10
        )
        gf.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(gf, bg='#2e2e2e')
        header.pack(fill=tk.X, pady=(0, 6))

        tk.Label(header, text="  PLAYER 1 LANE  ",
                 bg='#1a3a5c', fg='#00aaff',
                 font=('Arial', 11, 'bold'),
                 relief=tk.GROOVE, bd=2, padx=8, pady=4
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))

        tk.Label(header, text="  PLAYER 2 LANE  ",
                 bg='#5c1a3a', fg='#ff00aa',
                 font=('Arial', 11, 'bold'),
                 relief=tk.GROOVE, bd=2, padx=8, pady=4
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0))

        tiles_frame = tk.Frame(gf, bg='#2e2e2e')
        tiles_frame.pack()

        for row_idx, tile_row in enumerate(self.GRID_LAYOUT):
            tk.Label(
                tiles_frame, text=f"R{row_idx}",
                bg='#2e2e2e', fg='#888888',
                font=('Arial', 8), width=3
            ).grid(row=row_idx, column=0, padx=(0, 4))

            for col_idx, tile_num in enumerate(tile_row):
                lbl = tk.Label(
                    tiles_frame,
                    text=str(tile_num),
                    width=7, height=3,
                    bg='#000000', fg='#555555',
                    font=('Arial', 11, 'bold'),
                    relief=tk.RAISED, bd=2
                )
                lbl.grid(row=row_idx, column=col_idx + 1, padx=3, pady=3)
                self.tile_labels[tile_num] = lbl

    def _build_controls(self, parent):
        # Setup
        sf = tk.LabelFrame(parent, text="Setup",
                           bg='#2e2e2e', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=10)
        sf.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(sf, text="FULL RESET\nGenerate New Paths",
                  command=self.full_reset,
                  bg='#aa5500', fg='white',
                  font=('Arial', 11, 'bold'), height=3, width=22
        ).pack(pady=5)

        tk.Button(sf, text="START PREVIEW\nShow Paths",
                  command=self.start_preview,
                  bg='#0066ff', fg='white',
                  font=('Arial', 11, 'bold'), height=3, width=22
        ).pack(pady=5)

        # Gameplay
        gf = tk.LabelFrame(parent, text="Gameplay",
                           bg='#2e2e2e', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=10)
        gf.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(gf, text="NEXT STEP",
                  command=self.next_step,
                  bg='#00cc00', fg='white',
                  font=('Arial', 15, 'bold'), height=4, width=22
        ).pack(pady=8)

        # Player resets
        pf = tk.LabelFrame(parent, text="Player Resets",
                           bg='#2e2e2e', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=10)
        pf.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(pf, text="Reset Player 1",
                  command=self.reset_player1,
                  bg='#00aaff', fg='white',
                  font=('Arial', 10, 'bold'), height=2, width=22
        ).pack(pady=4)

        tk.Button(pf, text="Reset Player 2",
                  command=self.reset_player2,
                  bg='#ff00aa', fg='white',
                  font=('Arial', 10, 'bold'), height=2, width=22
        ).pack(pady=4)

        # Game status
        gs = tk.LabelFrame(parent, text="Game Status",
                           bg='#2e2e2e', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=10)
        gs.pack(fill=tk.X, padx=10, pady=5)

        self.p1_status_label = tk.Label(gs, text="Player 1: Row 0 / 10",
                                        bg='#2e2e2e', fg='#00aaff',
                                        font=('Arial', 10, 'bold'), anchor='w')
        self.p1_status_label.pack(fill=tk.X, pady=2)

        self.p2_status_label = tk.Label(gs, text="Player 2: Row 0 / 10",
                                        bg='#2e2e2e', fg='#ff00aa',
                                        font=('Arial', 10, 'bold'), anchor='w')
        self.p2_status_label.pack(fill=tk.X, pady=2)

        # Legend
        lf = tk.LabelFrame(parent, text="Legend",
                           bg='#2e2e2e', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=8)
        lf.pack(fill=tk.X, padx=10, pady=5)

        for text, color in [("  Correct path", '#00ff00'),
                             ("  Wrong tile",   '#ff0000'),
                             ("  Off / cleared",'#555555')]:
            tk.Label(lf, text=text, bg='#2e2e2e', fg=color,
                     font=('Arial', 9, 'bold'), anchor='w').pack(fill=tk.X, pady=1)

    # ── Back to menu ─────────────────────────────────────────────────────────
    def _back_to_menu(self):
        # Stop preview thread
        self._stop_preview   = True
        self.preview_running = False
        self.game_active     = False
        # Clear floor via manager
        self.manager.clear_all()
        self.back_cb()

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _set(self, tile_num, color):
        self.manager.send_color(tile_num, *color)
        hex_c = "#{:02x}{:02x}{:02x}".format(*color)
        if tile_num in self.tile_labels:
            self.tile_labels[tile_num].config(
                bg=hex_c,
                fg='#000000' if sum(color) > 200 else '#555555'
            )

    def _update_status_display(self):
        self.p1_status_label.config(text=f"Player 1: Row {self.p1_step} / 10")
        self.p2_status_label.config(text=f"Player 2: Row {self.p2_step} / 10")

    def _reset_grid_gui(self):
        for lbl in self.tile_labels.values():
            lbl.config(bg='#000000', fg='#555555')

    # ── Game logic ────────────────────────────────────────────────────────────
    def full_reset(self):
        self.logger.info("Full Reset – generating new paths")
        self.p1_step = 0
        self.p2_step = 0
        self.p1_path = [random.randint(0, 1) for _ in range(10)]
        self.p2_path = [random.randint(2, 3) for _ in range(10)]
        self.logger.info(f"P1 path: {self.p1_path}")
        self.logger.info(f"P2 path: {self.p2_path}")
        self._clear_floor()
        self._update_status_display()
        self.status_label.config(text="New paths generated! Press Start Preview to see them.")

    def start_preview(self):
        if self.preview_running:
            self.status_label.config(text="Preview already running...")
            return
        if not self.p1_path or not self.p2_path:
            self.status_label.config(text="No paths! Press Full Reset first.")
            return
        self._stop_preview = False
        threading.Thread(target=self._run_preview, daemon=True).start()

    def _run_preview(self):
        self.preview_running = True
        self.after(0, lambda: self.status_label.config(text="Preview starting..."))
        time.sleep(0.5)

        for row_idx in range(10):
            if self._stop_preview:
                break
            p1_col  = self.p1_path[row_idx]
            p2_col  = self.p2_path[row_idx]
            p1_tile = self.GRID_LAYOUT[row_idx][p1_col]
            p2_tile = self.GRID_LAYOUT[row_idx][p2_col]

            self.after(0, lambda t=p1_tile: self._set(t, self.COLOR_GREEN))
            self.after(0, lambda t=p2_tile: self._set(t, self.COLOR_GREEN))
            time.sleep(0.2)

        if not self._stop_preview:
            self.after(0, lambda: self.status_label.config(text="Memorize the path!"))
            time.sleep(1)
            self.after(0, lambda: self.status_label.config(text="Floor cleared – Ready to play!"))
            self._clear_floor()

        self.preview_running = False

    def next_step(self):
        if self.preview_running:
            self.status_label.config(text="Wait for preview to finish.")
            return
        if not self.p1_path or not self.p2_path:
            self.status_label.config(text="No paths! Press Full Reset first.")
            return

        # Clear previous row for P1
        if self.p1_step > 0:
            for col in [0, 1]:
                tile = self.GRID_LAYOUT[self.p1_step - 1][col]
                self._set(tile, self.COLOR_BLACK)

        # Clear previous row for P2
        if self.p2_step > 0:
            for col in [2, 3]:
                tile = self.GRID_LAYOUT[self.p2_step - 1][col]
                self._set(tile, self.COLOR_BLACK)

        time.sleep(0.1)

        # Player 1
        if self.p1_step < 10:
            row_idx       = self.p1_step
            correct_col   = self.p1_path[row_idx]
            incorrect_col = 1 - correct_col
            self._set(self.GRID_LAYOUT[row_idx][correct_col],   self.COLOR_GREEN)
            self._set(self.GRID_LAYOUT[row_idx][incorrect_col], self.COLOR_RED)
            self.p1_step += 1

        # Player 2
        if self.p2_step < 10:
            row_idx       = self.p2_step
            correct_col   = self.p2_path[row_idx]
            incorrect_col = 5 - correct_col
            self._set(self.GRID_LAYOUT[row_idx][correct_col],   self.COLOR_GREEN)
            self._set(self.GRID_LAYOUT[row_idx][incorrect_col], self.COLOR_RED)
            self.p2_step += 1

        self._update_status_display()

        if self.p1_step >= 10 and self.p2_step >= 10:
            self.status_label.config(text="Game Complete! Both players finished!")
        else:
            self.status_label.config(text=f"Step:  P1 = {self.p1_step}/10     P2 = {self.p2_step}/10")

    def reset_player1(self):
        self.logger.info("Reset Player 1")
        for row_idx in range(10):
            for col in [0, 1]:
                tile = self.GRID_LAYOUT[row_idx][col]
                self._set(tile, self.COLOR_BLACK)
        self.p1_step = 0
        self._update_status_display()
        self.status_label.config(text="Player 1 reset to Row 0")

    def reset_player2(self):
        self.logger.info("Reset Player 2")
        for row_idx in range(10):
            for col in [2, 3]:
                tile = self.GRID_LAYOUT[row_idx][col]
                self._set(tile, self.COLOR_BLACK)
        self.p2_step = 0
        self._update_status_display()
        self.status_label.config(text="Player 2 reset to Row 0")

    def _clear_floor(self):
        for row in self.GRID_LAYOUT:
            for tile_num in row:
                self._set(tile_num, self.COLOR_BLACK)


# ─────────────────────────────────────────────────────────────────────────────
#  MINES FRAME
# ─────────────────────────────────────────────────────────────────────────────
class MinesFrame(tk.Frame):
    """
    Mines Game UI encapsulated in a Frame.
    Tile layout: Row 0 = tiles 1-4 (bottom),  Row 9 = tiles 37-40 (top).
    """

    GRID_LAYOUT = [list(range(r * 4 + 1, r * 4 + 5)) for r in range(10)]
    # Row 0: [1,2,3,4], Row 1: [5,6,7,8], ..., Row 9: [37,38,39,40]

    ROUND_CONFIG = [
        {"round": 1, "eliminate": 10},
        {"round": 2, "eliminate": 10},
        {"round": 3, "eliminate":  5},
        {"round": 4, "eliminate":  5},
        {"round": 5, "eliminate":  3},
        {"round": 6, "eliminate":  3},
        {"round": 7, "eliminate":  2},
        {"round": 8, "eliminate":  1},
    ]

    def __init__(self, parent, manager: FloorConnectionManager, back_callback):
        super().__init__(parent, bg='#1a1a1a')
        self.manager = manager
        self.back_cb = back_callback
        self.logger  = logging.getLogger('MinesGame')

        self.total_tiles = manager.total_tiles

        self.current_round    = 0
        self.game_active      = False
        self.animation_active = False
        self.eliminated_tiles: set = set()
        self.winner_tile      = None

        # Local tile state strings for animation ('cyan'|'red'|'green'|'off')
        self._tile_state_str: dict = {}

        self.animation_thread = None
        self.tile_labels: dict = {}

        self._build_ui()
        self._start_animation()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        outer = tk.Frame(self, bg='#1a1a1a')
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # LEFT – game grid
        left_panel = tk.Frame(outer, bg='#1a1a1a')
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # MIDDLE – controls (fixed width)
        mid_panel = tk.Frame(outer, bg='#2a2a2a', relief=tk.RAISED, bd=3, width=280)
        mid_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        mid_panel.pack_propagate(False)

        self._build_grid(left_panel)
        self._build_controls(mid_panel)

    def _build_grid(self, parent):
        gf = tk.LabelFrame(
            parent,
            text=f"MINES GAME  –  {self.total_tiles} Players     "
                 "Row 0: tiles 1-4  →  Row 9: tiles 37-40",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 11, 'bold'), padx=6, pady=6
        )
        gf.pack(fill=tk.BOTH, expand=True)

        # Back button in grid header area
        back_btn = tk.Button(
            gf, text="◀  Back to Menu",
            command=self._back_to_menu,
            bg='#555555', fg='white',
            font=('Arial', 9, 'bold')
        )
        back_btn.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 6))

        # Column headers
        for c in range(4):
            tk.Label(
                gf, text=f"Col {c+1}",
                bg='#2a2a2a', fg='#888888',
                font=('Arial', 8)
            ).grid(row=1, column=c + 1, pady=(0, 3))

        for row_idx, tile_row in enumerate(self.GRID_LAYOUT):
            tk.Label(
                gf, text=f"R{row_idx}",
                bg='#2a2a2a', fg='#888888',
                font=('Arial', 8), width=3, anchor='e'
            ).grid(row=row_idx + 2, column=0, padx=(0, 3))

            for col_idx, tile_num in enumerate(tile_row):
                lbl = tk.Label(
                    gf,
                    text=str(tile_num),
                    width=8, height=3,
                    bg='#00ffff', fg='black',
                    font=('Arial', 12, 'bold'),
                    relief=tk.RAISED, bd=3
                )
                lbl.grid(row=row_idx + 2, column=col_idx + 1,
                         padx=4, pady=4, sticky='nsew')
                self.tile_labels[tile_num] = lbl
                self._tile_state_str[tile_num] = 'cyan'

        for c in range(1, 5):
            gf.columnconfigure(c, weight=1)
        for r in range(2, 12):
            gf.rowconfigure(r, weight=1)

    def _build_controls(self, parent):
        tk.Label(parent, text="GAME CONTROL",
                 bg='#2a2a2a', fg='#00ff00',
                 font=('Arial', 15, 'bold')
        ).pack(pady=(12, 4))

        # Status
        sf = tk.LabelFrame(parent, text="Game Status",
                           bg='#2a2a2a', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=8)
        sf.pack(fill=tk.X, padx=10, pady=5)

        self.round_label = tk.Label(sf, text="Round: Ready to Start",
                                    bg='#2a2a2a', fg='#ffff00',
                                    font=('Arial', 11, 'bold'),
                                    wraplength=240, justify='center')
        self.round_label.pack(pady=3)

        self.players_label = tk.Label(sf,
                                      text=f"Players Remaining: {self.total_tiles}",
                                      bg='#2a2a2a', fg='#00ff00',
                                      font=('Arial', 10, 'bold'))
        self.players_label.pack(pady=3)

        self.eliminated_label = tk.Label(sf, text="Eliminated This Round: 0",
                                         bg='#2a2a2a', fg='#ff6666',
                                         font=('Arial', 10))
        self.eliminated_label.pack(pady=3)

        # Buttons
        cf = tk.LabelFrame(parent, text="Controls",
                           bg='#2a2a2a', fg='white',
                           font=('Arial', 10, 'bold'), padx=10, pady=8)
        cf.pack(fill=tk.X, padx=10, pady=5)

        self.start_btn = tk.Button(cf, text="START GAME",
                                   command=self.start_game,
                                   bg='#00aa00', fg='white',
                                   font=('Arial', 12, 'bold'), height=2,
                                   relief=tk.RAISED, bd=3)
        self.start_btn.pack(fill=tk.X, pady=4)

        self.next_round_btn = tk.Button(cf, text="NEXT ROUND",
                                        command=self.next_round,
                                        bg='#ff8800', fg='white',
                                        font=('Arial', 12, 'bold'), height=2,
                                        relief=tk.RAISED, bd=3,
                                        state=tk.DISABLED)
        self.next_round_btn.pack(fill=tk.X, pady=4)

        self.reset_btn = tk.Button(cf, text="RESET GAME",
                                   command=self.reset_game,
                                   bg='#cc0000', fg='white',
                                   font=('Arial', 11, 'bold'), height=2,
                                   relief=tk.RAISED, bd=3)
        self.reset_btn.pack(fill=tk.X, pady=4)

        # Round schedule
        inf = tk.LabelFrame(parent, text="Round Schedule",
                            bg='#2a2a2a', fg='white',
                            font=('Arial', 10, 'bold'), padx=8, pady=6)
        inf.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        txt = tk.Text(inf, bg='#1a1a1a', fg='#00ff00',
                      font=('Courier', 8), wrap=tk.WORD,
                      relief=tk.SUNKEN, bd=2)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert('1.0',
            "R1: -10  →  30 left\n"
            "R2: -10  →  20 left\n"
            "R3:  -5  →  15 left\n"
            "R4:  -5  →  10 left\n"
            "R5:  -3  →   7 left\n"
            "R6:  -3  →   4 left\n"
            "R7:  -2  →   2 left\n"
            "R8:  -1  →  WINNER!\n\n"
            "CYAN  = Active\n"
            "RED   = Eliminated\n"
            "GREEN = Winner\n"
        )
        txt.config(state=tk.DISABLED)

    # ── Back to menu ─────────────────────────────────────────────────────────
    def _back_to_menu(self):
        self.game_active = False
        self._stop_animation()
        self.manager.clear_all()
        self.back_cb()

    # ── Cyan animation ────────────────────────────────────────────────────────
    def _start_animation(self):
        if self.animation_thread and self.animation_thread.is_alive():
            return
        self.animation_active = True
        self.animation_thread = threading.Thread(
            target=self._cyan_loop, daemon=True)
        self.animation_thread.start()

    def _stop_animation(self):
        self.animation_active = False
        if self.animation_thread:
            self.animation_thread.join(timeout=2)

    def _cyan_loop(self):
        while self.animation_active:
            try:
                for offset in range(self.total_tiles):
                    if not self.animation_active:
                        break
                    for tile_num in range(1, self.total_tiles + 1):
                        if tile_num in self.eliminated_tiles:
                            continue
                        d = abs((tile_num - 1) - (offset % self.total_tiles))
                        if d > self.total_tiles // 2:
                            d = self.total_tiles - d
                        if d < 5:
                            b = int(255 * (1 - d / 5))
                            self.manager.send_color(tile_num, 0, b, b)
                            self._tile_state_str[tile_num] = 'cyan'
                            if tile_num in self.tile_labels:
                                self.tile_labels[tile_num].config(
                                    bg=f"#00{b:02x}{b:02x}",
                                    fg='black' if b > 128 else 'white'
                                )
                        else:
                            self.manager.send_color(tile_num, 0, 0, 0)
                            self._tile_state_str[tile_num] = 'off'
                            if tile_num in self.tile_labels:
                                self.tile_labels[tile_num].config(
                                    bg='#1a1a1a', fg='#666666')
                    time.sleep(0.1)
            except Exception as e:
                self.logger.error(f"Animation error: {e}")
                time.sleep(1)

        # Turn off all non-eliminated tiles when animation stops
        for t in range(1, self.total_tiles + 1):
            if t not in self.eliminated_tiles:
                self.manager.send_color(t, 0, 0, 0)

    # ── Game logic ────────────────────────────────────────────────────────────
    def start_game(self):
        if self.game_active:
            messagebox.showwarning("Game Active", "Game is already in progress!")
            return

        self.current_round = 0
        self.eliminated_tiles.clear()
        self.winner_tile   = None
        self.game_active   = True
        self._stop_animation()
        time.sleep(0.3)

        for t in range(1, self.total_tiles + 1):
            self.manager.send_color(t, 0, 255, 255)
            self._tile_state_str[t] = 'cyan'
            if t in self.tile_labels:
                self.tile_labels[t].config(bg='#00ffff', fg='black')

        self.start_btn.config(state=tk.DISABLED)
        self.next_round_btn.config(state=tk.NORMAL)
        self.round_label.config(text="Ready – Press NEXT ROUND")
        self.players_label.config(text=f"Players Remaining: {self.total_tiles}")

        messagebox.showinfo(
            "Game Started",
            "Game started!\nAll 40 players are active.\nPress NEXT ROUND to begin Round 1."
        )

    def next_round(self):
        if not self.game_active:
            return
        if self.current_round >= len(self.ROUND_CONFIG):
            messagebox.showinfo("Game Over", "Game has ended!")
            return

        ri  = self.ROUND_CONFIG[self.current_round]
        rn  = ri['round']
        cnt = ri['eliminate']
        self.current_round += 1
        self.round_label.config(text=f"Round {rn}  –  Eliminating {cnt}...")

        available = [t for t in range(1, self.total_tiles + 1)
                     if t not in self.eliminated_tiles]
        if len(available) < cnt:
            messagebox.showerror("Error", "Not enough players remaining!")
            return

        for tile_num in random.sample(available, cnt):
            time.sleep(0.3)
            self.manager.send_color(tile_num, 0, 0, 0)
            self._tile_state_str[tile_num] = 'off'
            if tile_num in self.tile_labels:
                self.tile_labels[tile_num].config(bg='#1a1a1a', fg='#666666')

            time.sleep(0.1)
            self.manager.send_color(tile_num, 255, 0, 0)
            self._tile_state_str[tile_num] = 'red'
            if tile_num in self.tile_labels:
                self.tile_labels[tile_num].config(bg='#cc0000', fg='white')

            self.eliminated_tiles.add(tile_num)
            self.logger.info(f"Eliminated tile {tile_num}")

        remaining = len(available) - cnt
        self.players_label.config(text=f"Players Remaining: {remaining}")
        self.eliminated_label.config(text=f"Eliminated This Round: {cnt}")

        if self.current_round == len(self.ROUND_CONFIG):
            winners = [t for t in range(1, self.total_tiles + 1)
                       if t not in self.eliminated_tiles]
            if len(winners) == 1:
                w = winners[0]
                self.winner_tile = w
                time.sleep(1)
                self.manager.send_color(w, 0, 255, 0)
                self._tile_state_str[w] = 'green'
                if w in self.tile_labels:
                    self.tile_labels[w].config(bg='#00ff00', fg='black')
                self.logger.info(f"WINNER: Tile {w}")
                self.round_label.config(text=f"GAME OVER – WINNER: Tile {w}!")
                threading.Thread(target=self._flash_winner, args=(w,), daemon=True).start()
                messagebox.showinfo("WINNER!", f"GAME OVER!\n\nWINNER: Player on Tile {w}!\n\nCongratulations!")
                self.next_round_btn.config(state=tk.DISABLED)
                self.game_active = False
            else:
                messagebox.showerror("Error", f"Expected 1 winner, got {len(winners)}")
        else:
            self.round_label.config(text=f"Round {rn} done  –  {remaining} players remain")

    def _flash_winner(self, tile_num):
        for _ in range(10):
            self.manager.send_color(tile_num, 0, 255, 0)
            time.sleep(0.3)
            self.manager.send_color(tile_num, 255, 255, 255)
            time.sleep(0.3)
        self.manager.send_color(tile_num, 0, 255, 0)

    def reset_game(self):
        if self.game_active:
            if not messagebox.askyesno("Reset", "Are you sure you want to reset?"):
                return

        self.game_active = False
        self._stop_animation()
        time.sleep(0.3)

        self.current_round = 0
        self.eliminated_tiles.clear()
        self.winner_tile = None

        for t in range(1, self.total_tiles + 1):
            self.manager.send_color(t, 0, 0, 0)
            self._tile_state_str[t] = 'off'
            if t in self.tile_labels:
                self.tile_labels[t].config(bg='#3e3e3e', fg='white')

        time.sleep(0.4)
        self._start_animation()

        self.start_btn.config(state=tk.NORMAL)
        self.next_round_btn.config(state=tk.DISABLED)
        self.round_label.config(text="Round: Ready to Start")
        self.players_label.config(text=f"Players Remaining: {self.total_tiles}")
        self.eliminated_label.config(text="Eliminated This Round: 0")


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class UnifiedApp:
    """
    Root window that hosts:
      - A centre content area that swaps between Main Menu / Memory Path / Mines
      - A persistent right-side ESP Status Panel (always visible)
    """

    def __init__(self):
        self.logger = logging.getLogger('UnifiedApp')
        self.logger.info("=" * 60)
        self.logger.info("UNIFIED LED FLOOR SYSTEM – STARTING")
        self.logger.info("=" * 60)

        # Shared connection manager
        self.manager = FloorConnectionManager()

        # Root window
        self.root = tk.Tk()
        self.root.title("Unified LED Floor System")
        self.root.geometry("1600x950")
        self.root.resizable(True, True)
        self.root.configure(bg='#1a1a1a')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Outer layout: content on left, ESP panel on right
        outer = tk.Frame(self.root, bg='#1a1a1a')
        outer.pack(fill=tk.BOTH, expand=True)

        # RIGHT – persistent ESP status panel (fixed 290 px)
        self._right_panel = tk.Frame(outer, bg='#2a2a2a',
                                     relief=tk.RAISED, bd=2, width=290)
        self._right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 8), pady=8)
        self._right_panel.pack_propagate(False)
        self._build_esp_panel(self._right_panel)

        # LEFT – content area (swappable)
        self._content_area = tk.Frame(outer, bg='#1a1a1a')
        self._content_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                padx=(8, 0), pady=8)

        # Wire manager callback to GUI update (thread-safe via after())
        self.manager._on_status_change = lambda: self.root.after(0, self._refresh_esp_panel)

        # Game frames (created lazily)
        self._memory_frame: MemoryPathFrame | None = None
        self._mines_frame:  MinesFrame | None      = None
        self._current_frame = None

        self._show_main_menu()

        # Initial ESP panel update
        self.root.after(1500, self._refresh_esp_panel)

    # ── Main menu ─────────────────────────────────────────────────────────────
    def _show_main_menu(self):
        self._swap_content(None)  # clear existing game frame

        menu = tk.Frame(self._content_area, bg='#1a1a1a')
        menu.pack(fill=tk.BOTH, expand=True)
        self._current_frame = menu

        tk.Label(
            menu,
            text="LED FLOOR SYSTEM",
            bg='#1a1a1a', fg='#00ff00',
            font=('Arial', 36, 'bold')
        ).pack(pady=(80, 10))

        tk.Label(
            menu,
            text="Select a game to begin",
            bg='#1a1a1a', fg='#aaaaaa',
            font=('Arial', 16)
        ).pack(pady=(0, 60))

        btn_frame = tk.Frame(menu, bg='#1a1a1a')
        btn_frame.pack()

        tk.Button(
            btn_frame,
            text="🧠  MEMORY PATH GAME",
            command=self._launch_memory_path,
            bg='#005599', fg='white',
            font=('Arial', 18, 'bold'),
            width=28, height=4,
            relief=tk.RAISED, bd=4
        ).pack(pady=15)

        tk.Button(
            btn_frame,
            text="💣  MINES GAME",
            command=self._launch_mines,
            bg='#880000', fg='white',
            font=('Arial', 18, 'bold'),
            width=28, height=4,
            relief=tk.RAISED, bd=4
        ).pack(pady=15)

    def _swap_content(self, new_frame):
        """Destroy any existing content and place new_frame."""
        if self._current_frame is not None:
            self._current_frame.destroy()
            self._current_frame = None
        self._memory_frame = None
        self._mines_frame  = None
        if new_frame is not None:
            new_frame.pack(fill=tk.BOTH, expand=True)
            self._current_frame = new_frame

    def _launch_memory_path(self):
        self._swap_content(None)
        frame = MemoryPathFrame(self._content_area, self.manager, self._show_main_menu)
        frame.pack(fill=tk.BOTH, expand=True)
        self._memory_frame  = frame
        self._current_frame = frame

    def _launch_mines(self):
        self._swap_content(None)
        frame = MinesFrame(self._content_area, self.manager, self._show_main_menu)
        frame.pack(fill=tk.BOTH, expand=True)
        self._mines_frame   = frame
        self._current_frame = frame

    # ── ESP status panel (persistent right side) ──────────────────────────────
    def _build_esp_panel(self, parent):
        # Emergency stop button – most prominent element
        tk.Button(
            parent,
            text="⛔  CLEAR FLOOR\nSTOP ALL",
            command=self._emergency_stop,
            bg='#cc0000', fg='white',
            font=('Arial', 13, 'bold'),
            height=3, width=24,
            relief=tk.RAISED, bd=4
        ).pack(padx=8, pady=(12, 6), fill=tk.X)

        tk.Label(
            parent, text="ESP32 STATUS",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 14, 'bold')
        ).pack(pady=(6, 2))

        self._esp_count_label = tk.Label(
            parent, text="0 / 0 online",
            bg='#2a2a2a', fg='white',
            font=('Arial', 11)
        )
        self._esp_count_label.pack(pady=(0, 4))

        # Scrollable controller list
        lf = tk.LabelFrame(parent, text="Controllers",
                           bg='#2a2a2a', fg='white',
                           font=('Arial', 9, 'bold'), padx=4, pady=4)
        lf.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        esp_canvas = tk.Canvas(lf, bg='#2a2a2a', highlightthickness=0)
        esp_scroll = tk.Scrollbar(lf, orient=tk.VERTICAL, command=esp_canvas.yview)
        self._esp_status_frame = tk.Frame(esp_canvas, bg='#2a2a2a')

        esp_canvas.configure(yscrollcommand=esp_scroll.set)
        esp_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        esp_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        esp_canvas.create_window((0, 0), window=self._esp_status_frame, anchor='nw')
        self._esp_status_frame.bind(
            '<Configure>',
            lambda e: esp_canvas.configure(scrollregion=esp_canvas.bbox('all'))
        )

        self._reconnect_status_label = tk.Label(
            parent, text="Auto-reconnect: active",
            bg='#2a2a2a', fg='#888888',
            font=('Arial', 8)
        )
        self._reconnect_status_label.pack(pady=(4, 2))

        tk.Button(
            parent,
            text="Reconnect All ESPs",
            command=self.manager.reconnect_all_manual,
            bg='#5bc0de', fg='white',
            font=('Arial', 9, 'bold')
        ).pack(padx=8, pady=(0, 10), fill=tk.X)

    def _refresh_esp_panel(self):
        """Rebuild the ESP controller rows (piano-tiles style)."""
        for w in self._esp_status_frame.winfo_children():
            w.destroy()

        online_count = 0
        for mac, tiles in self.manager.esp_map.items():
            status = self.manager.esp_status.get(mac, 'offline')
            if status == 'online':
                online_count += 1

            row = tk.Frame(self._esp_status_frame, bg='#3a3a3a',
                           relief=tk.RAISED, bd=1)
            row.pack(fill=tk.X, pady=2, padx=2)

            tk.Label(row, text=mac[-6:],
                     bg='#3a3a3a', fg='white',
                     font=('Arial', 9, 'bold'), width=8, anchor='w'
            ).pack(side=tk.LEFT, padx=4)

            badge_text  = "ON"  if status == 'online' else "OFF"
            badge_color = '#5cb85c' if status == 'online' else '#d9534f'
            tk.Label(row, text=badge_text,
                     bg=badge_color, fg='white',
                     font=('Arial', 8, 'bold'), width=4
            ).pack(side=tk.LEFT, padx=4)

            tk.Label(row, text=f"Tiles: {tiles}",
                     bg='#3a3a3a', fg='#cccccc',
                     font=('Arial', 7), anchor='w'
            ).pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)

        total = len(self.manager.esp_map)
        clr = '#00ff00' if online_count == total else \
              '#ffaa00' if online_count > 0 else '#ff4444'
        self._esp_count_label.config(
            text=f"{online_count} / {total} online", fg=clr)

        offline_macs = [m for m, s in self.manager.esp_status.items()
                        if s == 'offline']
        if offline_macs:
            self._reconnect_status_label.config(
                text=f"Reconnecting: {', '.join(m[-6:] for m in offline_macs[:2])}...",
                fg='#ffaa00')
        else:
            self._reconnect_status_label.config(
                text="Auto-reconnect: all online  ✓", fg='#00aa00')

        # Re-schedule periodic refresh
        self.root.after(3000, self._refresh_esp_panel)

    # ── Emergency stop ────────────────────────────────────────────────────────
    def _emergency_stop(self):
        self.logger.warning("EMERGENCY STOP triggered!")

        # Stop any active game threads
        if self._mines_frame:
            self._mines_frame.game_active = False
            self._mines_frame._stop_animation()

        if self._memory_frame:
            self._memory_frame._stop_preview  = True
            self._memory_frame.preview_running = False
            self._memory_frame.game_active     = False

        # Send OFF to all 40 tiles
        self.manager.clear_all()

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def _on_close(self):
        self.logger.info("Application closing...")
        self._emergency_stop()
        self.manager.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("UNIFIED LED FLOOR SYSTEM")
    print("Memory Path Game  +  Mines Game")
    print("=" * 60)
    print()

    try:
        app = UnifiedApp()
        app.run()
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        print("Please ensure esp_config.json exists at the configured path.")
        input("Press Enter to exit...")
