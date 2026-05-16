"""
MINES GAME - LED Floor Edition
40 players, elimination rounds, cyan animations
Uses the same ESP TCP server architecture

UI Changes:
  - Grid reflects physical floor layout:
      Row 0 → tiles  1- 4  (leftmost column = tile 1)
      Row 1 → tiles  5- 8
      ...
      Row 9 → tiles 37-40
  - Piano-tiles style ESP32 status panel on the far right (fixed 250 px)
  - Middle column holds all game controls (fixed 270 px)
  - Everything fits in 1600x900 – no scrolling needed
"""

import json
import socket
import tkinter as tk
from tkinter import messagebox
import threading
import time
import random
import logging


class MinesGame:
    def __init__(self):
        self.config_file = "D:\learning material\college\mechatron\WORKING5\esp_config.json"
        self.esp_port    = 8080

        self.esp_sockets = {}
        self.esp_status  = {}
        self.socket_lock = threading.Lock()

        self.load_config()

        # Game state
        self.current_round    = 0
        self.game_active      = False
        self.animation_active = True
        self.eliminated_tiles = set()
        self.winner_tile      = None
        self.tile_states      = {}   # tile_num -> 'cyan'|'red'|'green'|'off'

        self.round_config = [
            {"round": 1, "eliminate": 10},
            {"round": 2, "eliminate": 10},
            {"round": 3, "eliminate":  5},
            {"round": 4, "eliminate":  5},
            {"round": 5, "eliminate":  3},
            {"round": 6, "eliminate":  3},
            {"round": 7, "eliminate":  2},
            {"round": 8, "eliminate":  1},
        ]

        # Physical floor grid: row 0 = tiles 1-4, row 9 = tiles 37-40
        self.grid_layout = [
            list(range(row * 4 + 1, row * 4 + 5)) for row in range(10)
        ]

        self.animation_thread   = None
        self.reconnect_thread   = None
        self.reconnect_active   = True
        self.reconnect_interval = 5

        self.setup_logging()
        self.create_gui()

    # ─────────────────────────────────────────────────────────────────
    #  LOGGING
    # ─────────────────────────────────────────────────────────────────
    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[
                logging.FileHandler('mines_game.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("=" * 60)
        self.logger.info("MINES GAME - LED FLOOR EDITION")
        self.logger.info("=" * 60)

    # ─────────────────────────────────────────────────────────────────
    #  CONFIG
    # ─────────────────────────────────────────────────────────────────
    def load_config(self):
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)

            self.grid_rows   = config['grid']['rows']
            self.grid_cols   = config['grid']['cols']
            self.esp_map     = config['esps']
            self.total_tiles = self.grid_rows * self.grid_cols

            self.tile_to_esp = {}
            for mac, tiles in self.esp_map.items():
                for idx, tile_num in enumerate(tiles):
                    self.tile_to_esp[tile_num] = (mac, idx)

            for mac in self.esp_map.keys():
                self.esp_status[mac] = 'offline'

        except Exception as e:
            print(f"Error loading config: {e}")
            messagebox.showerror("Error", f"Error loading config: {e}")
            exit(1)

    # ─────────────────────────────────────────────────────────────────
    #  CONNECTION
    # ─────────────────────────────────────────────────────────────────
    def connect_to_esp(self, mac):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((f"esp-{mac}.local", self.esp_port))
            self.logger.info(f"Connected to {mac}")
            self.esp_status[mac] = 'online'
            if hasattr(self, 'root'):
                self.root.after(0, self.update_esp_status_gui)
            return sock
        except Exception as e:
            self.logger.error(f"Failed {mac}: {e}")
            self.esp_status[mac] = 'offline'
            return None

    def auto_connect_esps(self):
        self.logger.info("Connecting to ESPs...")
        time.sleep(1)
        for mac in self.esp_map.keys():
            with self.socket_lock:
                self.esp_sockets[mac] = self.connect_to_esp(mac)
            time.sleep(0.3)
        connected = sum(1 for s in self.esp_status.values() if s == 'online')
        self.logger.info(f"Connected {connected}/{len(self.esp_map)}")
        self.root.after(0, self.update_esp_status_gui)

    def reconnect_all(self):
        def _do():
            with self.socket_lock:
                for s in self.esp_sockets.values():
                    try:
                        s and s.close()
                    except Exception:
                        pass
                self.esp_sockets.clear()
            for mac in self.esp_map.keys():
                with self.socket_lock:
                    self.esp_sockets[mac] = self.connect_to_esp(mac)
                time.sleep(0.3)
            self.root.after(0, self.update_esp_status_gui)
        threading.Thread(target=_do, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────
    #  AUTO-RECONNECT
    # ─────────────────────────────────────────────────────────────────
    def start_reconnect_monitor(self):
        self.reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True)
        self.reconnect_thread.start()

    def _reconnect_loop(self):
        while self.reconnect_active:
            time.sleep(self.reconnect_interval)
            for mac in list(self.esp_map.keys()):
                if self.esp_status.get(mac) == 'online':
                    continue
                new_sock = self.connect_to_esp(mac)
                if new_sock:
                    with self.socket_lock:
                        self.esp_sockets[mac] = new_sock
                    self._restore_tiles_for_esp(mac)
                    self.root.after(0, self.update_esp_status_gui)

    def _restore_tiles_for_esp(self, mac):
        for tile_num in self.esp_map.get(mac, []):
            state = self.tile_states.get(tile_num, 'off')
            if   state == 'red':   self._raw_send(tile_num, "COLOR_255_0_0")
            elif state == 'green': self._raw_send(tile_num, "COLOR_0_255_0")
            elif state == 'cyan':  self._raw_send(tile_num, "COLOR_0_255_255")
            else:                  self._raw_send(tile_num, "OFF")
            time.sleep(0.05)

    # ─────────────────────────────────────────────────────────────────
    #  COMMAND SENDING
    # ─────────────────────────────────────────────────────────────────
    def _raw_send(self, tile_num, command):
        if tile_num not in self.tile_to_esp:
            return False
        mac, local_idx = self.tile_to_esp[tile_num]
        with self.socket_lock:
            sock = self.esp_sockets.get(mac)
            if not sock:
                return False
            try:
                sock.sendall(f"T{local_idx + 1}_{command}\n".encode())
                return True
            except Exception as e:
                self.logger.error(f"Send error {mac}: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                self.esp_sockets[mac] = None
                self.esp_status[mac]  = 'offline'
                self.root.after(0, self.update_esp_status_gui)
                return False

    def set_tile_color(self, tile_num, r, g, b):
        if tile_num in self.eliminated_tiles:
            return
        self._raw_send(tile_num, f"COLOR_{r}_{g}_{b}")
        if   r==255 and g==0   and b==0:   self.tile_states[tile_num] = 'red'
        elif r==0   and g==255 and b==0:   self.tile_states[tile_num] = 'green'
        elif r==0   and g==255 and b==255: self.tile_states[tile_num] = 'cyan'
        else:                               self.tile_states[tile_num] = 'custom'

    def turn_off_tile(self, tile_num):
        self.tile_states[tile_num] = 'off'
        self._raw_send(tile_num, "OFF")

    # ─────────────────────────────────────────────────────────────────
    #  GUI  –  three-column layout, fits 1600x900
    # ─────────────────────────────────────────────────────────────────
    def create_gui(self):
        self.root = tk.Tk()
        self.root.title("MINES GAME - LED Floor Edition")
        self.root.geometry("1600x900")
        self.root.resizable(True, True)
        self.root.configure(bg='#1a1a1a')

        outer = tk.Frame(self.root, bg='#1a1a1a')
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # RIGHT – ESP status panel (fixed 250 px)
        right_panel = tk.Frame(outer, bg='#2a2a2a', relief=tk.RAISED, bd=2, width=250)
        right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        right_panel.pack_propagate(False)

        # MIDDLE – game controls (fixed 270 px)
        mid_panel = tk.Frame(outer, bg='#2a2a2a', relief=tk.RAISED, bd=3, width=270)
        mid_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        mid_panel.pack_propagate(False)

        # LEFT – tile grid (all remaining space)
        left_panel = tk.Frame(outer, bg='#1a1a1a')
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.create_game_grid(left_panel)
        self.create_control_panel(mid_panel)
        self.create_esp_panel(right_panel)

        threading.Thread(target=self._startup_sequence, daemon=True).start()
        self.start_cyan_animation()

    def _startup_sequence(self):
        self.auto_connect_esps()
        self.start_reconnect_monitor()

    # ─────────────────────────────────────────────────────────────────
    #  GAME GRID
    #  Row 0 = tiles 1-4  |  Row 1 = tiles 5-8  |  …  |  Row 9 = tiles 37-40
    # ─────────────────────────────────────────────────────────────────
    def create_game_grid(self, parent):
        grid_frame = tk.LabelFrame(
            parent,
            text=f"MINES GAME  –  {self.total_tiles} Players"
                 "     Row 0: tiles 1-4  →  Row 9: tiles 37-40",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 11, 'bold'),
            padx=6, pady=6
        )
        grid_frame.pack(fill=tk.BOTH, expand=True)

        # Column headers
        tk.Label(grid_frame, text="", bg='#2a2a2a', width=3).grid(row=0, column=0)
        for c in range(4):
            tk.Label(
                grid_frame,
                text=f"Col {c+1}",
                bg='#2a2a2a', fg='#888888',
                font=('Arial', 8)
            ).grid(row=0, column=c + 1, pady=(0, 3))

        self.tile_labels = {}

        for row_idx, tile_row in enumerate(self.grid_layout):
            # Row label
            tk.Label(
                grid_frame,
                text=f"R{row_idx}",
                bg='#2a2a2a', fg='#888888',
                font=('Arial', 8), width=3, anchor='e'
            ).grid(row=row_idx + 1, column=0, padx=(0, 3))

            for col_idx, tile_num in enumerate(tile_row):
                lbl = tk.Label(
                    grid_frame,
                    text=str(tile_num),
                    width=8, height=3,
                    bg='#00ffff', fg='black',
                    font=('Arial', 12, 'bold'),
                    relief=tk.RAISED, bd=3
                )
                lbl.grid(row=row_idx + 1, column=col_idx + 1,
                         padx=4, pady=4, sticky='nsew')
                self.tile_labels[tile_num] = lbl
                self.tile_states[tile_num] = 'cyan'

        for c in range(1, 5):
            grid_frame.columnconfigure(c, weight=1)
        for r in range(1, 11):
            grid_frame.rowconfigure(r, weight=1)

    # ─────────────────────────────────────────────────────────────────
    #  CONTROL PANEL  (middle column)
    # ─────────────────────────────────────────────────────────────────
    def create_control_panel(self, parent):
        tk.Label(
            parent,
            text="GAME CONTROL",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 15, 'bold')
        ).pack(pady=(12, 4))

        # ── Game Status ───────────────────────────────────────────
        sf = tk.LabelFrame(
            parent, text="Game Status",
            bg='#2a2a2a', fg='white',
            font=('Arial', 10, 'bold'), padx=10, pady=8
        )
        sf.pack(fill=tk.X, padx=10, pady=5)

        self.round_label = tk.Label(
            sf, text="Round: Ready to Start",
            bg='#2a2a2a', fg='#ffff00',
            font=('Arial', 11, 'bold'),
            wraplength=230, justify='center'
        )
        self.round_label.pack(pady=3)

        self.players_label = tk.Label(
            sf, text=f"Players Remaining: {self.total_tiles}",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 10, 'bold')
        )
        self.players_label.pack(pady=3)

        self.eliminated_label = tk.Label(
            sf, text="Eliminated This Round: 0",
            bg='#2a2a2a', fg='#ff6666',
            font=('Arial', 10)
        )
        self.eliminated_label.pack(pady=3)

        # ── Buttons ───────────────────────────────────────────────
        cf = tk.LabelFrame(
            parent, text="Controls",
            bg='#2a2a2a', fg='white',
            font=('Arial', 10, 'bold'), padx=10, pady=8
        )
        cf.pack(fill=tk.X, padx=10, pady=5)

        self.start_btn = tk.Button(
            cf, text="START GAME",
            command=self.start_game,
            bg='#00aa00', fg='white',
            font=('Arial', 12, 'bold'), height=2, relief=tk.RAISED, bd=3
        )
        self.start_btn.pack(fill=tk.X, pady=4)

        self.next_round_btn = tk.Button(
            cf, text="NEXT ROUND",
            command=self.next_round,
            bg='#ff8800', fg='white',
            font=('Arial', 12, 'bold'), height=2, relief=tk.RAISED, bd=3,
            state=tk.DISABLED
        )
        self.next_round_btn.pack(fill=tk.X, pady=4)

        self.reset_btn = tk.Button(
            cf, text="RESET GAME",
            command=self.reset_game,
            bg='#cc0000', fg='white',
            font=('Arial', 11, 'bold'), height=2, relief=tk.RAISED, bd=3
        )
        self.reset_btn.pack(fill=tk.X, pady=4)

        # ── Round schedule ────────────────────────────────────────
        inf = tk.LabelFrame(
            parent, text="Round Schedule",
            bg='#2a2a2a', fg='white',
            font=('Arial', 10, 'bold'), padx=8, pady=6
        )
        inf.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        txt = tk.Text(
            inf,
            bg='#1a1a1a', fg='#00ff00',
            font=('Courier', 8),
            wrap=tk.WORD, relief=tk.SUNKEN, bd=2
        )
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert('1.0',
            "R1: -10  →  30 left\n"
            "R2: -10  →  20 left\n"
            "R3:  -5  →  15 left\n"
            "R4:  -5  →  10 left\n"
            "R5:  -3  →   7 left\n"
            "R6:  -3  →   4 left\n"
            "R7:  -2  →   2 left\n"
            "R8:  -1  →  WINNER!\n"
            "\n"
            "CYAN  = Active\n"
            "RED   = Eliminated\n"
            "GREEN = Winner\n"
        )
        txt.config(state=tk.DISABLED)

    # ─────────────────────────────────────────────────────────────────
    #  ESP STATUS PANEL  (right column – piano-tiles style)
    # ─────────────────────────────────────────────────────────────────
    def create_esp_panel(self, parent):
        tk.Label(
            parent,
            text="ESP32 STATUS",
            bg='#2a2a2a', fg='#00ff00',
            font=('Arial', 13, 'bold')
        ).pack(pady=(12, 4))

        self.esp_count_label = tk.Label(
            parent,
            text="0 / 0 online",
            bg='#2a2a2a', fg='white',
            font=('Arial', 10)
        )
        self.esp_count_label.pack(pady=(0, 6))

        # Scrollable controller list
        lf = tk.LabelFrame(
            parent, text="Controllers",
            bg='#2a2a2a', fg='white',
            font=('Arial', 9, 'bold'), padx=4, pady=4
        )
        lf.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        esp_canvas = tk.Canvas(lf, bg='#2a2a2a', highlightthickness=0)
        esp_scroll = tk.Scrollbar(lf, orient=tk.VERTICAL, command=esp_canvas.yview)
        self.esp_status_frame = tk.Frame(esp_canvas, bg='#2a2a2a')

        esp_canvas.configure(yscrollcommand=esp_scroll.set)
        esp_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        esp_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        esp_canvas.create_window((0, 0), window=self.esp_status_frame, anchor='nw')
        self.esp_status_frame.bind(
            '<Configure>',
            lambda e: esp_canvas.configure(scrollregion=esp_canvas.bbox('all'))
        )

        self.reconnect_status_label = tk.Label(
            parent,
            text="Auto-reconnect: active",
            bg='#2a2a2a', fg='#888888',
            font=('Arial', 8)
        )
        self.reconnect_status_label.pack(pady=(6, 2))

        tk.Button(
            parent,
            text="Reconnect All ESPs",
            command=self.reconnect_all,
            bg='#5bc0de', fg='white',
            font=('Arial', 9, 'bold')
        ).pack(padx=8, pady=(0, 10), fill=tk.X)

    def update_esp_status_gui(self):
        """Rebuild ESP rows (piano-tiles style)."""
        for w in self.esp_status_frame.winfo_children():
            w.destroy()

        online_count = 0
        for mac, tiles in self.esp_map.items():
            status = self.esp_status.get(mac, 'offline')
            if status == 'online':
                online_count += 1

            row = tk.Frame(self.esp_status_frame, bg='#3a3a3a', relief=tk.RAISED, bd=1)
            row.pack(fill=tk.X, pady=2, padx=2)

            tk.Label(
                row, text=mac[-6:],
                bg='#3a3a3a', fg='white',
                font=('Arial', 8, 'bold'), width=8, anchor='w'
            ).pack(side=tk.LEFT, padx=3)

            tk.Label(
                row,
                text="ON" if status == 'online' else "OFF",
                bg='#5cb85c' if status == 'online' else '#d9534f',
                fg='white',
                font=('Arial', 8, 'bold'), width=4
            ).pack(side=tk.LEFT, padx=3)

            tk.Label(
                row, text=f"{tiles}",
                bg='#3a3a3a', fg='#cccccc',
                font=('Arial', 7), anchor='w'
            ).pack(side=tk.LEFT, padx=3, fill=tk.X, expand=True)

        total = len(self.esp_map)
        clr   = '#00ff00' if online_count == total else '#ffaa00' if online_count > 0 else '#ff4444'
        self.esp_count_label.config(text=f"{online_count} / {total} online", fg=clr)

        offline_macs = [m for m, s in self.esp_status.items() if s == 'offline']
        if offline_macs:
            self.reconnect_status_label.config(
                text=f"Reconnecting: {', '.join(m[-6:] for m in offline_macs[:2])}...",
                fg='#ffaa00'
            )
        else:
            self.reconnect_status_label.config(text="All online  ✓", fg='#00aa00')

    # Keep old name working (called internally on socket drop)
    def update_status_display(self):
        self.update_esp_status_gui()

    # ─────────────────────────────────────────────────────────────────
    #  GAME LOGIC
    # ─────────────────────────────────────────────────────────────────
    def start_game(self):
        if self.game_active:
            messagebox.showwarning("Game Active", "Game is already in progress!")
            return

        self.current_round = 0
        self.eliminated_tiles.clear()
        self.winner_tile   = None
        self.game_active   = True
        self.animation_active = False
        time.sleep(0.3)

        for t in range(1, self.total_tiles + 1):
            self.set_tile_color(t, 0, 255, 255)
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
        if self.current_round >= len(self.round_config):
            messagebox.showinfo("Game Over", "Game has ended!")
            return

        ri  = self.round_config[self.current_round]
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
            time.sleep(0.3)                         # delay between tiles

            self._raw_send(tile_num, "OFF")
            self.tile_states[tile_num] = 'off'
            self.tile_labels[tile_num].config(bg='#1a1a1a', fg='#666666')

            time.sleep(0.1)                         # delay between OFF and RED

            self._raw_send(tile_num, "COLOR_255_0_0")
            self.tile_states[tile_num] = 'red'
            self.tile_labels[tile_num].config(bg='#cc0000', fg='white')

            self.eliminated_tiles.add(tile_num)
            self.logger.info(f"Eliminated tile {tile_num}")

        remaining = len(available) - cnt
        self.players_label.config(text=f"Players Remaining: {remaining}")
        self.eliminated_label.config(text=f"Eliminated This Round: {cnt}")

        if self.current_round == len(self.round_config):
            winners = [t for t in range(1, self.total_tiles + 1)
                       if t not in self.eliminated_tiles]
            if len(winners) == 1:
                w = winners[0]
                self.winner_tile = w
                time.sleep(1)
                self._raw_send(w, "COLOR_0_255_0")
                self.tile_states[w] = 'green'
                self.tile_labels[w].config(bg='#00ff00', fg='black')
                self.logger.info(f"WINNER: Tile {w}")
                self.round_label.config(text=f"GAME OVER – WINNER: Tile {w}!")
                threading.Thread(target=self.flash_winner, args=(w,), daemon=True).start()
                messagebox.showinfo("WINNER!", f"GAME OVER!\n\nWINNER: Player on Tile {w}!\n\nCongratulations!")
                self.next_round_btn.config(state=tk.DISABLED)
                self.game_active = False
            else:
                messagebox.showerror("Error", f"Expected 1 winner, got {len(winners)}")
        else:
            self.round_label.config(text=f"Round {rn} done  –  {remaining} players remain")

    def flash_winner(self, tile_num):
        for _ in range(10):
            self._raw_send(tile_num, "COLOR_0_255_0")
            self.tile_states[tile_num] = 'green'
            time.sleep(0.3)
            self._raw_send(tile_num, "COLOR_255_255_255")
            time.sleep(0.3)
        self._raw_send(tile_num, "COLOR_0_255_0")
        self.tile_states[tile_num] = 'green'

    def reset_game(self):
        if self.game_active:
            if not messagebox.askyesno("Reset", "Are you sure?"):
                return

        self.game_active = False
        self.current_round = 0
        self.eliminated_tiles.clear()
        self.winner_tile = None

        for t in range(1, self.total_tiles + 1):
            self.turn_off_tile(t)
            self.tile_labels[t].config(bg='#3e3e3e', fg='white')

        time.sleep(0.5)
        self.animation_active = True
        self.start_cyan_animation()

        self.start_btn.config(state=tk.NORMAL)
        self.next_round_btn.config(state=tk.DISABLED)
        self.round_label.config(text="Round: Ready to Start")
        self.players_label.config(text=f"Players Remaining: {self.total_tiles}")
        self.eliminated_label.config(text="Eliminated This Round: 0")

    # ─────────────────────────────────────────────────────────────────
    #  CYAN ANIMATION
    # ─────────────────────────────────────────────────────────────────
    def start_cyan_animation(self):
        if self.animation_thread and self.animation_thread.is_alive():
            return
        self.animation_active = True
        self.animation_thread = threading.Thread(
            target=self.cyan_animation_loop, daemon=True)
        self.animation_thread.start()

    def cyan_animation_loop(self):
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
                            self._raw_send(tile_num, f"COLOR_0_{b}_{b}")
                            self.tile_states[tile_num] = 'cyan'
                            self.tile_labels[tile_num].config(
                                bg=f"#00{b:02x}{b:02x}",
                                fg='black' if b > 128 else 'white'
                            )
                        else:
                            self._raw_send(tile_num, "OFF")
                            self.tile_states[tile_num] = 'off'
                            self.tile_labels[tile_num].config(bg='#1a1a1a', fg='#666666')
                    time.sleep(0.1)
            except Exception as e:
                self.logger.error(f"Animation error: {e}")
                time.sleep(1)

        for t in range(1, self.total_tiles + 1):
            self._raw_send(t, "OFF")

    # ─────────────────────────────────────────────────────────────────
    #  RUN
    # ─────────────────────────────────────────────────────────────────
    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.reconnect_active = False
            self.animation_active = False
            with self.socket_lock:
                for sock in self.esp_sockets.values():
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass


if __name__ == "__main__":
    print("=" * 60)
    print("MINES GAME - LED FLOOR EDITION")
    print("40 Players - Elimination Battle Royale")
    print("=" * 60)
    MinesGame().run()
