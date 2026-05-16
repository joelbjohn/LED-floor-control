"""
MEMORY PATH GAME - LED Floor Game
Dual-lane path memory game for 10x4 LED floor
Uses proven TCP architecture from led_floor_master_WORKING.py
"""

import json
import socket
import tkinter as tk
from tkinter import ttk
import threading
import time
import random
import logging

class MemoryPathGame:
    def __init__(self):
        # Configuration
        self.config_file = 'D:\learning material\college\mechatron\WORKING5\esp_config.json'
        self.esp_port = 8080
        
        # ESP connections (socket cache) - Initialize BEFORE load_config
        self.esp_sockets = {}  # MAC -> socket
        self.esp_status = {}   # MAC -> 'online'/'offline'
        self.socket_lock = threading.Lock()
        
        # Setup logging
        self.setup_logging()
        
        # Load configuration
        self.load_config()
        
        # Game state
        self.game_active = False
        self.preview_running = False

        # Auto-reconnect
        self.reconnect_active = True        # Kept alive for the app's lifetime
        self.reconnect_interval = 5         # Seconds between reconnect sweeps
        self.tile_states = {}               # tile_num -> (r,g,b) or None; mirrors live floor state
        
        # Player paths - each array holds column choice (0 or 1 for P1, 2 or 3 for P2)
        self.p1_path = []  # 10 values, each 0 or 1
        self.p2_path = []  # 10 values, each 2 or 3
        
        # Player progress
        self.p1_step = 0  # Current row (0-10)
        self.p2_step = 0  # Current row (0-10)
        
        # Colors
        self.color_green = (0, 255, 0)
        self.color_red = (255, 0, 0)
        self.color_black = (0, 0, 0)
        
        # Grid layout - 10 rows (top to bottom), 4 columns
        # Row 0 = top (tiles 37-40), Row 9 = bottom (tiles 1-4)
        self.grid_layout = [
            [37, 38, 39, 40],  # Row 0 - TOP
            [33, 34, 35, 36],  # Row 1
            [29, 30, 31, 32],  # Row 2
            [25, 26, 27, 28],  # Row 3
            [21, 22, 23, 24],  # Row 4
            [17, 18, 19, 20],  # Row 5
            [13, 14, 15, 16],  # Row 6
            [9, 10, 11, 12],   # Row 7
            [5, 6, 7, 8],      # Row 8
            [1, 2, 3, 4]       # Row 9 - BOTTOM
        ]
        
        # Create GUI
        self.create_gui()
        
    def setup_logging(self):
        """Setup logging"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[
                logging.FileHandler('memory_path.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("="*60)
        self.logger.info("MEMORY PATH GAME - LED FLOOR")
        self.logger.info("="*60)
        
    def load_config(self):
        """Load ESP configuration"""
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
                
            self.grid_rows = config['grid']['rows']
            self.grid_cols = config['grid']['cols']
            self.esp_map = config['esps']  # MAC -> tile list
            
            # Create tile to ESP mapping: tile_num -> (mac, local_index)
            self.tile_to_esp = {}
            for mac, tiles in self.esp_map.items():
                for idx, tile_num in enumerate(tiles):
                    self.tile_to_esp[tile_num] = (mac, idx)
                    
            print(f"✅ Loaded config: {self.grid_rows}x{self.grid_cols} grid")
            print(f"✅ {len(self.esp_map)} ESP32 controllers")
            
            # Initialize status
            for mac in self.esp_map.keys():
                self.esp_status[mac] = 'offline'
                
        except FileNotFoundError:
            print(f"❌ Config file not found: {self.config_file}")
            exit(1)
        except Exception as e:
            print(f"❌ Error loading config: {e}")
            exit(1)
            
    def connect_to_esp(self, mac):
        """Connect to ESP via mDNS hostname"""
        try:
            hostname = f"esp-{mac}.local"
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((hostname, self.esp_port))
            
            self.logger.info(f"✅ Connected to {mac} ({hostname})")
            self.esp_status[mac] = 'online'
            
            # Update GUI
            if hasattr(self, 'root'):
                self.root.after(0, self.update_connection_status)
            
            return sock
            
        except socket.gaierror:
            self.logger.error(f"❌ Cannot resolve {hostname} - check mDNS")
            self.esp_status[mac] = 'offline'
            return None
        except socket.timeout:
            self.logger.error(f"❌ Connection timeout to {hostname}")
            self.esp_status[mac] = 'offline'
            return None
        except Exception as e:
            self.logger.error(f"❌ Failed to connect to {mac}: {e}")
            self.esp_status[mac] = 'offline'
            return None
            
    def send_command(self, tile_num, command):
        """Send command to ESP controlling the tile"""
        if tile_num not in self.tile_to_esp:
            self.logger.warning(f"⚠️  Tile {tile_num} not in configuration")
            return False
            
        mac, local_idx = self.tile_to_esp[tile_num]
        
        with self.socket_lock:
            # Get or create connection
            if mac not in self.esp_sockets or self.esp_sockets[mac] is None:
                self.esp_sockets[mac] = self.connect_to_esp(mac)
            
            sock = self.esp_sockets[mac]
            if not sock:
                return False
            
            try:
                # Command format: T<local_tile>_<action>
                cmd = f"T{local_idx + 1}_{command}\n"
                sock.sendall(cmd.encode())
                return True
                
            except Exception as e:
                self.logger.error(f"❌ Error sending to {mac}: {e}")
                # Close dead socket
                try:
                    sock.close()
                except:
                    pass
                self.esp_sockets[mac] = None
                self.esp_status[mac] = 'offline'
                
                # Update GUI
                if hasattr(self, 'root'):
                    self.root.after(0, self.update_connection_status)
                
                return False
                
    def set_tile_color(self, tile_num, color):
        """Set a tile to a specific color and record its state for reconnect restore."""
        # Track the state so _restore_tiles_for_esp can replay it after reconnect
        self.tile_states[tile_num] = color if color and color != (0, 0, 0) else None

        if color == (0, 0, 0) or color is None:
            command = "OFF"
        else:
            r, g, b = color
            command = f"COLOR_{r}_{g}_{b}"

        return self.send_command(tile_num, command)
    
    def create_gui(self):
        """Create GUI"""
        self.root = tk.Tk()
        self.root.title("Memory Path Game - LED Floor")
        self.root.geometry("1600x900")
        self.root.configure(bg='#1e1e1e')
        
        # Main layout
        main_frame = tk.Frame(self.root, bg='#1e1e1e')
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Top - Title
        title_label = tk.Label(
            main_frame,
            text="MEMORY PATH GAME",
            bg='#1e1e1e',
            fg='#00ff00',
            font=('Arial', 28, 'bold')
        )
        title_label.pack(pady=10)
        
        # Center area - Grid and Controls
        center_frame = tk.Frame(main_frame, bg='#1e1e1e')
        center_frame.pack(fill=tk.BOTH, expand=True)
        
        # Left - Virtual Grid
        left_frame = tk.Frame(center_frame, bg='#1e1e1e')
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        
        # Right - Control Panel
        right_frame = tk.Frame(center_frame, bg='#2e2e2e', relief=tk.RAISED, bd=2)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        
        self.create_virtual_grid(left_frame)
        self.create_control_panel(right_frame)
        
        # Bottom - Status
        self.create_status_bar(main_frame)
        
        # Auto-connect to ESPs
        threading.Thread(target=self.auto_connect_esps, daemon=True).start()
        
    def create_virtual_grid(self, parent):
        """Create virtual grid display"""
        grid_frame = tk.LabelFrame(
            parent,
            text="LED Floor Grid (10x4)",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 14, 'bold'),
            padx=15,
            pady=15
        )
        grid_frame.pack(fill=tk.BOTH, expand=True)
        
        # Add lane labels
        lane_frame = tk.Frame(grid_frame, bg='#2e2e2e')
        lane_frame.pack(fill=tk.X, pady=(0, 10))
        
        tk.Label(
            lane_frame,
            text="PLAYER 1 LANE",
            bg='#2e2e2e',
            fg='#00aaff',
            font=('Arial', 12, 'bold'),
            width=20
        ).pack(side=tk.LEFT, padx=(0, 10))
        
        tk.Label(
            lane_frame,
            text="PLAYER 2 LANE",
            bg='#2e2e2e',
            fg='#ff00aa',
            font=('Arial', 12, 'bold'),
            width=20
        ).pack(side=tk.RIGHT, padx=(10, 0))
        
        # Create grid
        tiles_frame = tk.Frame(grid_frame, bg='#2e2e2e')
        tiles_frame.pack()
        
        self.tile_labels = {}
        
        # Create 10 rows x 4 columns
        for row_idx in range(10):
            for col_idx in range(4):
                tile_num = self.grid_layout[row_idx][col_idx]
                
                # Determine border color based on lane
                border_color = '#00aaff' if col_idx < 2 else '#ff00aa'
                
                tile = tk.Label(
                    tiles_frame,
                    text=str(tile_num),
                    width=10,
                    height=4,
                    bg='#000000',
                    fg='#555555',
                    font=('Arial', 10),
                    relief=tk.RAISED,
                    bd=3,
                    highlightthickness=2,
                    highlightbackground=border_color
                )
                tile.grid(row=row_idx, column=col_idx, padx=3, pady=3)
                self.tile_labels[tile_num] = tile
                
    def create_control_panel(self, parent):
        """Create control panel"""
        
        # Title
        tk.Label(
            parent,
            text="GAME CONTROLS",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 16, 'bold')
        ).pack(pady=15)
        
        # Full Reset Section
        reset_frame = tk.LabelFrame(
            parent,
            text="Game Setup",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=10
        )
        reset_frame.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Button(
            reset_frame,
            text="🔄 FULL RESET\nGenerate New Paths",
            command=self.full_reset,
            bg='#ff6600',
            fg='white',
            font=('Arial', 12, 'bold'),
            height=3,
            width=20
        ).pack(pady=5)
        
        tk.Button(
            reset_frame,
            text="▶️ START PREVIEW\nShow Paths",
            command=self.start_preview,
            bg='#0066ff',
            fg='white',
            font=('Arial', 12, 'bold'),
            height=3,
            width=20
        ).pack(pady=5)
        
        # Gameplay Section
        game_frame = tk.LabelFrame(
            parent,
            text="Gameplay",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=10
        )
        game_frame.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Button(
            game_frame,
            text="⬇️ NEXT STEP ⬇️",
            command=self.next_step,
            bg='#00cc00',
            fg='white',
            font=('Arial', 16, 'bold'),
            height=4,
            width=20
        ).pack(pady=10)
        
        # Player Reset Section
        player_frame = tk.LabelFrame(
            parent,
            text="Player Resets",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=10
        )
        player_frame.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Button(
            player_frame,
            text="🔄 Reset Player 1",
            command=self.reset_player1,
            bg='#00aaff',
            fg='white',
            font=('Arial', 11, 'bold'),
            height=2,
            width=20
        ).pack(pady=5)
        
        tk.Button(
            player_frame,
            text="🔄 Reset Player 2",
            command=self.reset_player2,
            bg='#ff00aa',
            fg='white',
            font=('Arial', 11, 'bold'),
            height=2,
            width=20
        ).pack(pady=5)
        
        # Game Info
        info_frame = tk.LabelFrame(
            parent,
            text="Game Status",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=10
        )
        info_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.p1_status_label = tk.Label(
            info_frame,
            text="Player 1: Row 0/10",
            bg='#2e2e2e',
            fg='#00aaff',
            font=('Arial', 10, 'bold'),
            anchor='w'
        )
        self.p1_status_label.pack(fill=tk.X, pady=2)
        
        self.p2_status_label = tk.Label(
            info_frame,
            text="Player 2: Row 0/10",
            bg='#2e2e2e',
            fg='#ff00aa',
            font=('Arial', 10, 'bold'),
            anchor='w'
        )
        self.p2_status_label.pack(fill=tk.X, pady=2)
        
        # Connection Status
        conn_frame = tk.LabelFrame(
            parent,
            text="ESP Connections",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 10, 'bold'),
            padx=10,
            pady=10
        )
        conn_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Scrollable area for connections
        canvas = tk.Canvas(conn_frame, bg='#2e2e2e', highlightthickness=0, height=200)
        scrollbar = tk.Scrollbar(conn_frame, orient=tk.VERTICAL, command=canvas.yview)
        self.conn_status_frame = tk.Frame(canvas, bg='#2e2e2e')
        
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.create_window((0, 0), window=self.conn_status_frame, anchor='nw')
        
        self.conn_status_frame.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
        )

        # Reconnect monitor status line (updated by _reconnect_loop via update_connection_status)
        self.reconnect_status_label = tk.Label(
            conn_frame,
            text="Auto-reconnect: active",
            bg='#2e2e2e',
            fg='#888888',
            font=('Arial', 8)
        )
        self.reconnect_status_label.pack(pady=(4, 0))
        
    def create_status_bar(self, parent):
        """Create status bar"""
        status_frame = tk.Frame(parent, bg='#2e2e2e', relief=tk.SUNKEN, bd=2)
        status_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.status_label = tk.Label(
            status_frame,
            text="Ready - Press Full Reset to start",
            bg='#2e2e2e',
            fg='white',
            font=('Arial', 11),
            anchor='w',
            padx=10,
            pady=5
        )
        self.status_label.pack(fill=tk.X)
        
    def update_virtual_grid(self):
        """Update virtual grid display"""
        # Reset all tiles to black
        for tile_num, label in self.tile_labels.items():
            label.config(bg='#000000', fg='#555555')
            
    def update_status_display(self):
        """Update player status display"""
        self.p1_status_label.config(text=f"Player 1: Row {self.p1_step}/10")
        self.p2_status_label.config(text=f"Player 2: Row {self.p2_step}/10")
        
    def update_connection_status(self):
        """Update ESP connection status display"""
        # Clear existing widgets
        for widget in self.conn_status_frame.winfo_children():
            widget.destroy()
        
        # Show connection status for each ESP
        online_count = 0
        for mac, tiles in self.esp_map.items():
            status = self.esp_status.get(mac, 'offline')
            if status == 'online':
                online_count += 1
            
            frame = tk.Frame(self.conn_status_frame, bg='#3e3e3e', relief=tk.RAISED, bd=1)
            frame.pack(fill=tk.X, pady=1, padx=2)
            
            # Status indicator
            status_color = '#00ff00' if status == 'online' else '#ff0000'
            tk.Label(
                frame,
                text="●",
                bg='#3e3e3e',
                fg=status_color,
                font=('Arial', 14, 'bold'),
                width=2
            ).pack(side=tk.LEFT, padx=2)
            
            # MAC address (last 6 chars)
            tk.Label(
                frame,
                text=mac[-6:],
                bg='#3e3e3e',
                fg='white',
                font=('Arial', 8),
                width=8,
                anchor='w'
            ).pack(side=tk.LEFT, padx=2)
            
            # Tiles
            tk.Label(
                frame,
                text=f"Tiles: {tiles}",
                bg='#3e3e3e',
                fg='white',
                font=('Arial', 7),
                anchor='w'
            ).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        
        # Update status bar
        total_esps = len(self.esp_map)
        if online_count == total_esps:
            conn_text = f"✅ All ESPs connected ({online_count}/{total_esps})"
        else:
            conn_text = f"⚠️  {online_count}/{total_esps} ESPs connected"

        if hasattr(self, 'status_label'):
            current_text = self.status_label.cget('text')
            if not self.preview_running and not self.game_active:
                self.status_label.config(text=conn_text)

        # Update reconnect monitor label
        if hasattr(self, 'reconnect_status_label'):
            offline_macs = [m for m, s in self.esp_status.items() if s == 'offline']
            if offline_macs:
                self.reconnect_status_label.config(
                    text=f"Reconnecting: {', '.join(m[-6:] for m in offline_macs[:2])}…",
                    fg='#ffaa00'
                )
            else:
                self.reconnect_status_label.config(
                    text="Auto-reconnect: all online ✓",
                    fg='#00aa00'
                )
        
    def auto_connect_esps(self):
        """Auto-connect to all ESPs on startup, then start the reconnect monitor."""
        self.logger.info("🔌 Connecting to ESPs...")
        time.sleep(1)

        for mac in self.esp_map.keys():
            sock = self.connect_to_esp(mac)
            with self.socket_lock:
                self.esp_sockets[mac] = sock
            time.sleep(0.3)

        connected = sum(1 for s in self.esp_status.values() if s == 'online')
        self.logger.info(f"✅ Connected to {connected}/{len(self.esp_map)} ESPs")

        self.root.after(0, self.update_connection_status)

        # Kick off the background reconnect monitor
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    # ------------------------------------------------------------------
    #  AUTO-RECONNECT
    # ------------------------------------------------------------------
    def _reconnect_loop(self):
        """
        Background thread: every `reconnect_interval` seconds it checks for
        offline ESPs and tries to reconnect them silently.  After a successful
        reconnect it re-sends the last known colour for every tile on that ESP
        so the physical floor stays in sync with the game state.
        Game flow is never blocked – this runs entirely in its own thread.
        """
        while self.reconnect_active:
            time.sleep(self.reconnect_interval)

            for mac in list(self.esp_map.keys()):
                if self.esp_status.get(mac) == 'online':
                    continue                        # Already connected – nothing to do

                self.logger.info(f"🔄 Auto-reconnect: attempting {mac}…")
                new_sock = self.connect_to_esp(mac)  # Updates esp_status internally

                if new_sock:
                    with self.socket_lock:
                        self.esp_sockets[mac] = new_sock

                    self.logger.info(f"✅ Reconnected {mac} – restoring tile colours")
                    self._restore_tiles_for_esp(mac)
                    self.root.after(0, self.update_connection_status)

    def _restore_tiles_for_esp(self, mac):
        """
        Re-send the last known colour command for every tile that lives on the
        newly reconnected ESP.  Reads from self.tile_states which is kept
        up-to-date by set_tile_color().
        """
        for tile_num in self.esp_map.get(mac, []):
            color = self.tile_states.get(tile_num)   # None means 'off'
            if color and color != (0, 0, 0):
                r, g, b = color
                cmd = f"COLOR_{r}_{g}_{b}"
            else:
                cmd = "OFF"

            # Use the raw socket path so we don't re-enter set_tile_color bookkeeping
            mac2, local_idx = self.tile_to_esp[tile_num]
            with self.socket_lock:
                sock = self.esp_sockets.get(mac2)
                if not sock:
                    continue
                try:
                    sock.sendall(f"T{local_idx + 1}_{cmd}\n".encode())
                except Exception as e:
                    self.logger.error(f"❌ Restore send error for tile {tile_num}: {e}")

            time.sleep(0.05)   # Small gap – avoids flooding the just-reconnected ESP
        
    def full_reset(self):
        """Full game reset - generate new paths"""
        self.logger.info("🔄 Full Reset - Generating new paths")
        
        # Reset progress
        self.p1_step = 0
        self.p2_step = 0
        
        # Generate new random paths
        self.p1_path = [random.randint(0, 1) for _ in range(10)]  # Cols 0 or 1
        self.p2_path = [random.randint(2, 3) for _ in range(10)]  # Cols 2 or 3
        
        self.logger.info(f"Player 1 Path: {self.p1_path}")
        self.logger.info(f"Player 2 Path: {self.p2_path}")
        
        # Clear the floor
        self.clear_floor()
        
        # Update displays
        self.update_status_display()
        self.status_label.config(text="New paths generated! Press Start Preview to see them.")
        
    def start_preview(self):
        """Start the preview animation"""
        if self.preview_running:
            self.logger.warning("Preview already running")
            return
        
        if not self.p1_path or not self.p2_path:
            self.status_label.config(text="⚠️  No paths! Press Full Reset first.")
            return
        
        # Run preview in thread
        threading.Thread(target=self._run_preview, daemon=True).start()
        
    def _run_preview(self):
        """Run preview animation (in thread)"""
        self.preview_running = True
        self.root.after(0, lambda: self.status_label.config(text="🎬 Preview starting..."))
        
        time.sleep(0.5)
        
        # Cascade green path from top to bottom
        for row_idx in range(10):
            # Light up correct tiles in this row
            p1_col = self.p1_path[row_idx]
            p2_col = self.p2_path[row_idx]
            
            p1_tile = self.grid_layout[row_idx][p1_col]
            p2_tile = self.grid_layout[row_idx][p2_col]
            
            self.set_tile_color(p1_tile, self.color_green)
            self.set_tile_color(p2_tile, self.color_green)
            
            # Update GUI
            self.root.after(0, lambda t1=p1_tile, t2=p2_tile: self._update_tile_gui(t1, self.color_green, t2, self.color_green))
            
            time.sleep(0.3)
        
        # Hold full path for 2 seconds
        self.root.after(0, lambda: self.status_label.config(text="📍 Memorize the path!"))
        time.sleep(2)
        
        # Turn everything black
        self.root.after(0, lambda: self.status_label.config(text="💡 Floor cleared - Ready to play!"))
        self.clear_floor()
        
        self.preview_running = False
        
    def _update_tile_gui(self, tile1, color1, tile2, color2):
        """Update tile GUI colors"""
        if tile1 in self.tile_labels:
            self.tile_labels[tile1].config(bg=self._rgb_to_hex(color1))
        if tile2 in self.tile_labels:
            self.tile_labels[tile2].config(bg=self._rgb_to_hex(color2))
            
    def _rgb_to_hex(self, rgb):
        """Convert RGB tuple to hex color"""
        return "#{:02x}{:02x}{:02x}".format(*rgb)
        
    def next_step(self):
        """Progress both players to next step"""
        if self.preview_running:
            self.logger.warning("Wait for preview to finish")
            return
        
        if not self.p1_path or not self.p2_path:
            self.status_label.config(text="⚠️  No paths! Press Full Reset first.")
            return
        
        # Turn off previous row (NO TRAIL)
        if self.p1_step > 0:
            prev_row = self.p1_step - 1
            for col in [0, 1]:
                tile = self.grid_layout[prev_row][col]
                self.set_tile_color(tile, self.color_black)
                self.root.after(0, lambda t=tile: self.tile_labels[t].config(bg='#000000'))
        
        if self.p2_step > 0:
            prev_row = self.p2_step - 1
            for col in [2, 3]:
                tile = self.grid_layout[prev_row][col]
                self.set_tile_color(tile, self.color_black)
                self.root.after(0, lambda t=tile: self.tile_labels[t].config(bg='#000000'))
        
        time.sleep(0.1)
        
        # Show current step for Player 1
        if self.p1_step < 10:
            row_idx = self.p1_step
            correct_col = self.p1_path[row_idx]
            incorrect_col = 1 - correct_col  # If correct is 0, incorrect is 1, and vice versa
            
            correct_tile = self.grid_layout[row_idx][correct_col]
            incorrect_tile = self.grid_layout[row_idx][incorrect_col]
            
            self.set_tile_color(correct_tile, self.color_green)
            self.set_tile_color(incorrect_tile, self.color_red)
            
            self.root.after(0, lambda ct=correct_tile, it=incorrect_tile: (
                self.tile_labels[ct].config(bg=self._rgb_to_hex(self.color_green)),
                self.tile_labels[it].config(bg=self._rgb_to_hex(self.color_red))
            ))
            
            self.p1_step += 1
        
        # Show current step for Player 2
        if self.p2_step < 10:
            row_idx = self.p2_step
            correct_col = self.p2_path[row_idx]
            incorrect_col = 5 - correct_col  # If correct is 2, incorrect is 3, and vice versa
            
            correct_tile = self.grid_layout[row_idx][correct_col]
            incorrect_tile = self.grid_layout[row_idx][incorrect_col]
            
            self.set_tile_color(correct_tile, self.color_green)
            self.set_tile_color(incorrect_tile, self.color_red)
            
            self.root.after(0, lambda ct=correct_tile, it=incorrect_tile: (
                self.tile_labels[ct].config(bg=self._rgb_to_hex(self.color_green)),
                self.tile_labels[it].config(bg=self._rgb_to_hex(self.color_red))
            ))
            
            self.p2_step += 1
        
        # Update status
        self.update_status_display()
        
        if self.p1_step >= 10 and self.p2_step >= 10:
            self.status_label.config(text="🎉 Game Complete! Both players finished!")
        else:
            self.status_label.config(text=f"Step: P1={self.p1_step}/10, P2={self.p2_step}/10")
        
    def reset_player1(self):
        """Reset Player 1 only"""
        self.logger.info("🔄 Resetting Player 1")
        
        # Turn off all Player 1 tiles (columns 0 and 1)
        for row_idx in range(10):
            for col in [0, 1]:
                tile = self.grid_layout[row_idx][col]
                self.set_tile_color(tile, self.color_black)
                self.root.after(0, lambda t=tile: self.tile_labels[t].config(bg='#000000'))
        
        # Reset P1 step
        self.p1_step = 0
        self.update_status_display()
        self.status_label.config(text="Player 1 reset to Row 0")
        
    def reset_player2(self):
        """Reset Player 2 only"""
        self.logger.info("🔄 Resetting Player 2")
        
        # Turn off all Player 2 tiles (columns 2 and 3)
        for row_idx in range(10):
            for col in [2, 3]:
                tile = self.grid_layout[row_idx][col]
                self.set_tile_color(tile, self.color_black)
                self.root.after(0, lambda t=tile: self.tile_labels[t].config(bg='#000000'))
        
        # Reset P2 step
        self.p2_step = 0
        self.update_status_display()
        self.status_label.config(text="Player 2 reset to Row 0")
        
    def clear_floor(self):
        """Turn off all tiles"""
        for row in self.grid_layout:
            for tile_num in row:
                self.set_tile_color(tile_num, self.color_black)
        
        # Update GUI
        self.root.after(0, self.update_virtual_grid)
        
    def run(self):
        """Run the application"""
        try:
            self.root.mainloop()
        finally:
            self.reconnect_active = False   # Signal reconnect thread to stop
            # Cleanup connections
            with self.socket_lock:
                for sock in self.esp_sockets.values():
                    if sock:
                        try:
                            sock.close()
                        except:
                            pass

if __name__ == "__main__":
    print("="*60)
    print("MEMORY PATH GAME - LED FLOOR")
    print("="*60)
    print()
    print("Game Rules:")
    print("- Two players, each with their own lane (2 columns)")
    print("- Each lane has a random path of correct tiles")
    print("- Preview shows the full path in green")
    print("- During play, green = correct, red = incorrect")
    print("- Players must remember the path!")
    print("="*60)
    print()
    
    game = MemoryPathGame()
    game.run()
