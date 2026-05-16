"""
MINES GAME - LED Floor Edition
40 players, elimination rounds, cyan animations
OPTIMIZED: Auto reconnect + state resync + safer threading
"""

import json
import socket
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import random
import logging
import sys


class MinesGame:
    def __init__(self):
        # ================= CONFIG =================
        self.config_file = r"D:\learning material\college\mechatron\WORKING5\esp_config.json"
        self.esp_port = 8080

        # ESP connection data
        self.esp_sockets = {}
        self.esp_status = {}
        self.socket_lock = threading.Lock()
        self.reconnect_interval = 5
        self.running = True

        # Load configuration
        self.load_config()

        # ================= GAME STATE =================
        self.current_round = 0
        self.game_active = False
        self.animation_active = True
        self.eliminated_tiles = set()
        self.tile_state_memory = {}

        self.round_config = [
            {"round": 1, "eliminate": 10, "color": "red"},
            {"round": 2, "eliminate": 10, "color": "red"},
            {"round": 3, "eliminate": 5, "color": "red"},
            {"round": 4, "eliminate": 5, "color": "red"},
            {"round": 5, "eliminate": 3, "color": "red"},
            {"round": 6, "eliminate": 3, "color": "red"},
            {"round": 7, "eliminate": 2, "color": "red"},
            {"round": 8, "eliminate": 1, "color": "red"},
        ]

        self.animation_thread = None

        self.setup_logging()
        self.create_gui()

        # Background connection manager
        threading.Thread(target=self.connection_manager, daemon=True).start()

    # =====================================================
    # LOGGING
    # =====================================================

    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[logging.FileHandler('mines_game.log'),
                      logging.StreamHandler()]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("=" * 60)
        self.logger.info("MINES GAME - LED FLOOR EDITION (OPTIMIZED)")
        self.logger.info("=" * 60)

    # =====================================================
    # CONFIG
    # =====================================================

    def load_config(self):
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)

            self.grid_rows = config['grid']['rows']
            self.grid_cols = config['grid']['cols']
            self.esp_map = config['esps']

            self.tile_to_esp = {}
            for mac, tiles in self.esp_map.items():
                for idx, tile_num in enumerate(tiles):
                    self.tile_to_esp[tile_num] = (mac, idx)

            self.total_tiles = self.grid_rows * self.grid_cols

            for mac in self.esp_map.keys():
                self.esp_status[mac] = 'offline'
                self.esp_sockets[mac] = None

            print(f"✅ Loaded config: {self.total_tiles} tiles")

        except Exception as e:
            print(f"❌ Config load error: {e}")
            sys.exit(1)

    # =====================================================
    # CONNECTION MANAGEMENT
    # =====================================================

    def connect_to_esp(self, mac):
        try:
            hostname = f"esp-{mac}.local"
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((hostname, self.esp_port))
            sock.settimeout(None)

            self.logger.info(f"Connected → {mac}")
            self.esp_status[mac] = 'online'

            self.resync_esp(mac)
            return sock

        except:
            self.esp_status[mac] = 'offline'
            return None

    def connection_manager(self):
        while self.running:
            with self.socket_lock:
                for mac in self.esp_map:
                    if self.esp_sockets[mac] is None:
                        self.esp_sockets[mac] = self.connect_to_esp(mac)
            self.root.after(0, self.update_status_display)
            time.sleep(self.reconnect_interval)

    def resync_esp(self, mac):
        for tile_num, state in self.tile_state_memory.items():
            tile_mac, local_idx = self.tile_to_esp[tile_num]
            if tile_mac == mac:
                try:
                    cmd = f"T{local_idx + 1}_{state}\n"
                    self.esp_sockets[mac].sendall(cmd.encode())
                except:
                    pass

    # =====================================================
    # COMMAND SENDING
    # =====================================================

    def send_command(self, tile_num, command):
        if tile_num not in self.tile_to_esp:
            return False

        mac, local_idx = self.tile_to_esp[tile_num]

        with self.socket_lock:
            sock = self.esp_sockets.get(mac)

            if not sock:
                return False

            try:
                cmd = f"T{local_idx + 1}_{command}\n"
                sock.sendall(cmd.encode())
                return True
            except:
                try:
                    sock.close()
                except:
                    pass
                self.esp_sockets[mac] = None
                self.esp_status[mac] = 'offline'
                return False

    # =====================================================
    # TILE CONTROL (MEMORY ADDED)
    # =====================================================

    def set_tile_color(self, tile_num, r, g, b):
        if tile_num in self.eliminated_tiles:
            return
        state = f"COLOR_{r}_{g}_{b}"
        self.tile_state_memory[tile_num] = state
        self.send_command(tile_num, state)

    def turn_off_tile(self, tile_num):
        self.tile_state_memory[tile_num] = "OFF"
        self.send_command(tile_num, "OFF")

    # =====================================================
    # GUI
    # =====================================================

    def create_gui(self):
        self.root = tk.Tk()
        self.root.title("MINES GAME - LED Floor Edition")
        self.root.geometry("1600x900")
        self.root.configure(bg='#1a1a1a')

        main_frame = tk.Frame(self.root, bg='#1a1a1a')
        main_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        left_frame = tk.Frame(main_frame, bg='#1a1a1a')
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right_frame = tk.Frame(main_frame, bg='#2a2a2a')
        right_frame.pack(side=tk.RIGHT, fill=tk.Y)
        right_frame.config(width=400)

        self.create_game_grid(left_frame)
        self.create_control_panel(right_frame)

        self.start_cyan_animation()

    # =====================================================
    # GAME GRID
    # =====================================================

    def create_game_grid(self, parent):
        grid_frame = tk.Frame(parent, bg='#2a2a2a')
        grid_frame.pack(fill=tk.BOTH, expand=True)

        self.tile_labels = {}
        tile_num = 1

        for row in range(self.grid_rows):
            for col in range(self.grid_cols):
                label = tk.Label(
                    grid_frame,
                    text=str(tile_num),
                    width=7,
                    height=3,
                    bg='#00ffff',
                    font=('Arial', 14, 'bold'),
                    relief=tk.RAISED,
                    bd=3
                )
                label.grid(row=row, column=col, padx=3, pady=3, sticky='nsew')
                self.tile_labels[tile_num] = label
                tile_num += 1

    # =====================================================
    # CONTROL PANEL
    # =====================================================

    def create_control_panel(self, parent):
        tk.Button(parent, text="START GAME",
                  command=self.start_game,
                  bg='#00aa00').pack(pady=10)

        tk.Button(parent, text="NEXT ROUND",
                  command=self.next_round,
                  bg='#ff8800').pack(pady=10)

        tk.Button(parent, text="RESET",
                  command=self.reset_game,
                  bg='#cc0000').pack(pady=10)

        self.status_label = tk.Label(parent, text="Connecting...")
        self.status_label.pack(pady=20)

    # =====================================================
    # GAME LOGIC (UNCHANGED)
    # =====================================================

    def start_game(self):
        self.current_round = 0
        self.eliminated_tiles.clear()
        self.tile_state_memory.clear()
        self.game_active = True

        for tile in range(1, self.total_tiles + 1):
            self.set_tile_color(tile, 0, 255, 255)
            self.tile_labels[tile].config(bg='#00ffff')

    def next_round(self):
        if not self.game_active:
            return

        if self.current_round >= len(self.round_config):
            return

        eliminate = self.round_config[self.current_round]["eliminate"]
        self.current_round += 1

        available = [t for t in range(1, self.total_tiles + 1)
                     if t not in self.eliminated_tiles]

        selected = random.sample(available, eliminate)

        for tile in selected:
            self.set_tile_color(tile, 255, 0, 0)
            self.tile_labels[tile].config(bg='#ff0000')
            self.eliminated_tiles.add(tile)
            time.sleep(0.3)

        if self.current_round == len(self.round_config):
            winner = [t for t in range(1, self.total_tiles + 1)
                      if t not in self.eliminated_tiles][0]

            self.set_tile_color(winner, 0, 255, 0)
            self.tile_labels[winner].config(bg='#00ff00')
            self.game_active = False

    def reset_game(self):
        self.game_active = False
        self.current_round = 0
        self.eliminated_tiles.clear()
        self.tile_state_memory.clear()

        for tile in range(1, self.total_tiles + 1):
            self.turn_off_tile(tile)
            self.tile_labels[tile].config(bg='#333333')

    # =====================================================
    # CYAN ANIMATION
    # =====================================================

    def start_cyan_animation(self):
        if self.animation_thread and self.animation_thread.is_alive():
            return
        self.animation_active = True
        self.animation_thread = threading.Thread(target=self.cyan_animation_loop, daemon=True)
        self.animation_thread.start()

    def cyan_animation_loop(self):
        while self.animation_active:
            for tile in range(1, self.total_tiles + 1):
                if tile not in self.eliminated_tiles:
                    self.set_tile_color(tile, 0, 255, 255)
            time.sleep(0.5)

    # =====================================================
    # STATUS UPDATE
    # =====================================================

    def update_status_display(self):
        online = sum(1 for s in self.esp_status.values() if s == 'online')
        total = len(self.esp_status)
        self.status_label.config(text=f"ESP32: {online}/{total} online")

    # =====================================================
    # SHUTDOWN
    # =====================================================

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.running = False
            self.animation_active = False
            with self.socket_lock:
                for sock in self.esp_sockets.values():
                    if sock:
                        try:
                            sock.close()
                        except:
                            pass


if __name__ == "__main__":
    game = MinesGame()
    game.run()