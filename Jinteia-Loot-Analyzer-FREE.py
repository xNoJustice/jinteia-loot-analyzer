#!/usr/bin/env python3
import datetime as dt
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional, Iterable, List, Deque, Dict, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import math

# ---------------------------------------------------------------------------
# Parsing and data structures
# ---------------------------------------------------------------------------

DUNGEONS = {
    "Razador": {
        "items": ["Truhe des Razador", "Razador's Chest"],
        "color": "#7f1d1d",
    },
    "Nemere": {
        "items": ["Truhe des Nemere", "Nemere's Chest"],
        "color": "#1e3a8a",
    },
    "Jotun": {
        "items": ["Truhe des Jotun Thrym", "Jotun Thrym's Chest"],
        "color": "#14532d",
    },
    "Blue Death": {
        "items": ["Truhe der Höllfenpforte", "Truhe der Höllenpforte", "Hellgates Chest"],
        "color": "#0c4a6e",
    },
    "Natuhu": {
        "items": ["Truhe des Eulen-König", "Chest of the Owl King"],
        "color": "#581c87",
    },
    "Taliko": {
        "items": ["Talikos Reichtum", "Taliko's Riches"],
        "color": "#92400e",
    },
    "Affengott": {
        "items": ["Affengottes Schatz", "Monkey God's Treasure"],
        "color": "#065f46",
    },
    "Nalantir": {
        "items": ["Nalantirs Vermächtnis", "Nalantir's Legacy"],
        "color": "#4c1d95",
    },
    "Nozdormu": {
        "items": ["Schatz der Dornen", "Treasure of Thorns"],
        "color": "#991b1b",
    },
}

LOG_LINE_RE = re.compile(
    r"\[(\d{2}/\d{2}/\d{2})\] \[(\d{2}:\d{2}:\d{2})\]: You receive (\d+) (.+?)\."
)

@dataclass
class LootEvent:
    ts: dt.datetime
    quantity: int
    item: str

    @property
    def is_yang(self) -> bool:
        return self.item == "Yang"

def parse_datetime_from_log(date_str: str, time_str: str) -> dt.datetime:
    """Parse date/time from the log format: 24/11/25 00:29:29."""
    return dt.datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %H:%M:%S")

def parse_log_line(line: str) -> Optional[LootEvent]:
    """Parse a single log line into a LootEvent, or return None if it does not match."""
    m = LOG_LINE_RE.search(line)
    if not m:
        return None
    date_str, time_str, qty_str, item = m.groups()
    ts = parse_datetime_from_log(date_str, time_str)
    quantity = int(qty_str)
    return LootEvent(ts=ts, quantity=quantity, item=item)
def iter_events_from_file(path: str) -> Iterable[LootEvent]:
    """Iterate over all events in the given log file."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            ev = parse_log_line(line)
            if ev:
                yield ev

def stats_from_events(events: Iterable[LootEvent]) -> Dict:
    """Compute statistics from a list/iterable of events."""
    dropped_yang = 0
    items_qty = defaultdict(int)
    events_list: List[LootEvent] = []

    for ev in events:
        events_list.append(ev)
        if ev.is_yang:
            dropped_yang += ev.quantity
        else:
            items_qty[ev.item] += ev.quantity

    if not events_list:
        return {
            "dropped_yang": 0,
            "items_qty": {},
            "hours": 0.0,
        }

    start = events_list[0].ts
    end = events_list[-1].ts
    elapsed_seconds = max((end - start).total_seconds(), 1)
    hours = elapsed_seconds / 3600.0

    return {
        "dropped_yang": dropped_yang,
        "items_qty": dict(items_qty),
        "hours": hours,
        "start": start,
        "end": end,
    }

# ---------------------------------------------------------------------------
# Live monitor worker (background thread)
# ---------------------------------------------------------------------------

class LiveMonitorWorker(threading.Thread):
    """
    Background thread that tails the log file and maintains a sliding window
    of the last N minutes. It periodically calls update_callback(stats_dict).
    """

    def __init__(
        self,
        path: str,
        window_minutes: int,
        refresh_secs: int,
        from_start: bool,
        update_callback,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.path = path
        self.window_minutes = window_minutes
        self.refresh_secs = refresh_secs
        self.from_start = from_start
        self.update_callback = update_callback
        self.stop_event = stop_event

        self.window: Deque[LootEvent] = deque()

    def add_event(self, ev: LootEvent, ignore_cutoff: bool = False):
        self.window.append(ev)
        if not ignore_cutoff:
            cutoff = ev.ts - dt.timedelta(minutes=self.window_minutes)
            while self.window and self.window[0].ts < cutoff:
                self.window.popleft()

    def compute_stats_from_window(self) -> Optional[Dict]:
        if not self.window:
            return None

        events_list = list(self.window)
        dropped_yang = sum(ev.quantity for ev in events_list if ev.is_yang)
        items_qty: Dict[str, int] = defaultdict(int)

        # Track Dungeon Runs
        dungeon_runs = defaultdict(int)
        total_runs = 0

        for ev in events_list:
            if not ev.is_yang:
                items_qty[ev.item] += ev.quantity
                # Check if item belongs to a dungeon chest
                for d_name, d_data in DUNGEONS.items():
                    if ev.item in d_data["items"]:
                        dungeon_runs[d_name] += ev.quantity
                        total_runs += ev.quantity

        start = events_list[0].ts
        end = events_list[-1].ts
        elapsed = max((end - start).total_seconds(), 1)
        hours = elapsed / 3600.0
        minutes = elapsed / 60.0

        # Build per-item stats including per-hour (rounded to int)
        items_list: List[Tuple[str, int, int, int]] = []
        total_item_value = 0

        prices = getattr(self, 'price_db', {})

        for name, qty in items_qty.items():
            per_hour = int(round(qty / hours))
            price = prices.get(name, 0)
            item_value = qty * price
            total_item_value += item_value
            items_list.append((name, qty, per_hour, item_value))

        # Sort by quantity desc
        items_list.sort(key=lambda x: x[1], reverse=True)

        stats = {
            "start": start,
            "end": end,
            "hours": hours,
            "minutes": minutes,
            "dropped_yang": dropped_yang,
            "yang_per_hour": int(round(dropped_yang / hours)),
            "yang_per_minute": int(round(dropped_yang / minutes)),
            "items": items_list,
            "dungeon_runs": dict(dungeon_runs),
            "total_dungeon_runs": total_runs
        }
        return stats

    def run(self):
        try:
            self.update_callback({"status": "Opening log file..."})
            f = open(self.path, "r", encoding="utf-8", errors="ignore")
        except OSError as e:
            # Send error to UI via callback as None with extra key
            self.update_callback({"error": f"Cannot open log file: {e}"})
            return

        if self.from_start:
            self.update_callback({"status": "Reading historical data (this may take a moment)..."})
            # Read all existing lines
            for line in f:
                ev = parse_log_line(line)
                if ev:
                    # Pass True to keep historical data during the initial load
                    self.add_event(ev, ignore_cutoff=True)


            self.update_callback({"status": "Historical data loaded. Starting live monitor."})
            # Update UI immediately after reading historical data
            stats = self.compute_stats_from_window()
            if stats:
                self.update_callback(stats)
        else:
            # Jump to the end if we only want live data
            f.seek(0, os.SEEK_END)
            self.update_callback({"status": "Monitoring live logs..."})

        last_print = time.time()

        while not self.stop_event.is_set():
            line = f.readline()
            if not line:
                # no new data
                time.sleep(0.2)
            else:
                ev = parse_log_line(line)
                if ev:
                    self.add_event(ev)

            now_ts = time.time()
            if now_ts - last_print >= self.refresh_secs:
                last_print = now_ts
                stats = self.compute_stats_from_window()
                if stats is not None:
                    self.update_callback(stats)

        f.close()
        self.update_callback({"status": "Monitoring stopped."})

# ---------------------------------------------------------------------------
# Tkinter UI - Modern Dark Theme
# ---------------------------------------------------------------------------

class LootMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("💰 Jinteia Real-time Yang & Loot Tracker")
        self.geometry("700x800")

        # Set dark theme colors
        self.bg_color = "#1a1a2e"
        self.card_bg = "#16213e"
        self.accent_color = "#0fcc45"
        self.accent_secondary = "#0ea5e9"
        self.text_color = "#e2e8f0"
        self.muted_text = "#94a3b8"

        self.configure(bg=self.bg_color)

        # Configure styles
        self.style = ttk.Style(self)
        self.style.theme_use("clam")

        # Configure ttk styles
        self.style.configure("TFrame", background=self.bg_color)
        self.style.configure("TLabelframe", background=self.bg_color, relief="flat", borderwidth=0)
        self.style.configure("TLabelframe.Label", background=self.card_bg, foreground=self.text_color,
                           font=("Segoe UI", 11, "bold"), padding=(10, 5))
        self.style.configure("TLabel", background=self.bg_color, foreground=self.text_color,
                           font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground=self.accent_color)
        self.style.configure("Stats.TLabel", font=("Segoe UI", 18, "bold"), foreground=self.accent_secondary)

        # Button styles
        self.style.configure("TNotebook", background=self.bg_color, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=self.card_bg, foreground=self.text_color, padding=[10, 5])
        self.style.map("TNotebook.Tab", background=[("selected", self.accent_secondary)], foreground=[("selected", "white")])
        self.style.configure("Accent.TButton", background=self.accent_color, foreground="white",
                           font=("Segoe UI", 10, "bold"), borderwidth=0, padding=10)
        self.style.map("Accent.TButton",
                      background=[("active", "#0db33d"), ("disabled", "#4a5568")])

        self.style.configure("Secondary.TButton", background="#4a5568", foreground="white",
                           font=("Segoe UI", 10), borderwidth=0, padding=8)

        # Treeview styles
        self.style.configure("Treeview", background="#2d3748", foreground=self.text_color,
                           fieldbackground="#2d3748", borderwidth=0, font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", background="#1e293b", foreground=self.accent_secondary,
                           font=("Segoe UI", 10, "bold"), borderwidth=0)
        self.style.map("Treeview", background=[("selected", "#4a5568")])
        self.style.map("Treeview",
                        background=[('selected', '#3b82f6')], # A nice blue for selection
                        foreground=[('selected', 'white')]
                    )

        self.stop_event = threading.Event()
        self.worker: Optional[LiveMonitorWorker] = None
        self.last_received_stats = None

        self.load_bookmarks()
        self.load_prices()
        self.create_widgets()

    # -------------------- UI layout -------------------- #

    def create_widgets(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # --- TAB 1: DASHBOARD ---
        self.tab_main = tk.Frame(self.notebook, bg=self.bg_color)
        self.notebook.add(self.tab_main, text=" 🎮 Dashboard ")

        # Controls Card
        controls_card = tk.Frame(self.tab_main, bg=self.card_bg, relief="flat", borderwidth=0)
        controls_card.pack(fill="x", pady=(0, 5))

        # --- Updated Settings Header with Log Selection ---
        controls_header = tk.Frame(controls_card, bg=self.card_bg)
        controls_header.pack(fill="x", padx=20, pady=(15, 5))

        tk.Label(controls_header, text="Controls", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 11, "bold")).pack(side="left")

        # Controls content
        controls_content = tk.Frame(controls_card, bg=self.card_bg)
        controls_content.pack(fill="x", padx=20, pady=(0, 20))

        # Control buttons
        control_buttons = tk.Frame(controls_content, bg=self.card_bg)
        control_buttons.pack(fill="x", pady=(15, 0))

        self.start_button = ttk.Button(control_buttons, text="▶ Start Monitoring",
                                      command=self.start_monitor, style="Accent.TButton")
        self.start_button.pack(side="left", padx=(0, 10))

        self.stop_button = ttk.Button(control_buttons, text="⏹ Stop",
                                     command=self.stop_monitor, style="Secondary.TButton",
                                     state="disabled")
        self.stop_button.pack(side="left")

        self.mini_btn = ttk.Button(control_buttons, text="📱 Mini Mode",
                                  command=self.toggle_mini_window, style="Secondary.TButton")
        self.mini_btn.pack(side="left", padx=10)

        # Stats Dashboard
        stats_card = tk.Frame(self.tab_main, bg=self.card_bg, relief="flat", borderwidth=0)
        stats_card.pack(fill="x", pady=(0, 5))

        # Stats header
        stats_header = tk.Frame(stats_card, bg=self.card_bg)
        stats_header.pack(fill="x", padx=20, pady=(15, 1))
        tk.Label(stats_header, text="📊 Live Statistics", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 11, "bold")).pack(side="left")

        # Stats grid - Fully vertical alignment for time and currency
        stats_grid = tk.Frame(stats_card, bg=self.card_bg)
        stats_grid.pack(fill="x", padx=20, pady=(0, 20))

        # Helper function to make adding rows easier
        def add_stat_row(label_text, row_idx, text_color):
            lbl = tk.Label(stats_grid, text=label_text, bg=self.card_bg, fg=self.muted_text,
                          font=("Segoe UI", 10, "bold"))
            lbl.grid(row=row_idx, column=0, sticky="w", pady=2)

            val = tk.Label(stats_grid, text="---", bg=self.card_bg, fg=text_color,
                          font=("Segoe UI", 10))
            val.grid(row=row_idx, column=1, sticky="w", padx=10)
            return val

        # Row 0: Interval
        self.interval_label = add_stat_row("Interval:", 0, self.text_color)

        # Row 1: Window
        self.window_length_label = add_stat_row("Window:", 1, self.text_color)

        # Row 2: Total Yang
        self.yang_label = add_stat_row("Dropped Yang:", 2, self.accent_color)
        self.yang_label.config(font=("Segoe UI", 11, "bold")) # Make the numbers slightly pop

        # Row 3: Yang / Hour
        self.yang_per_hour_label = add_stat_row("Dropped Yang / Hour:", 3, self.accent_secondary)
        self.yang_per_hour_label.config(font=("Segoe UI", 11, "bold"))

        # Row 4: Yang / Minute
        self.yang_per_minute_label = add_stat_row("Dropped Yang / Minute:", 4, "#f59e0b")
        self.yang_per_minute_label.config(font=("Segoe UI", 11, "bold"))

        # Row 5: Total Item Value (Net Worth)
        self.material_value_label = add_stat_row("Total Items Value:", 5, "#a855f7") # Purple color for profit
        self.material_value_label.config(font=("Segoe UI", 11, "bold"))

        # --- DUNGEON BLOCKS (NEW) ---
        dungeon_container = tk.Frame(self.tab_main, bg=self.card_bg, pady=10)
        dungeon_container.pack(fill="x", padx=10)

        tk.Label(dungeon_container, text="🏰 Dungeon Runs", bg=self.card_bg, fg=self.text_color, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10)

        # The frame that will hold the dynamic blocks
        self.dungeon_blocks = tk.Frame(dungeon_container, bg=self.card_bg)
        self.dungeon_blocks.pack(fill="x", pady=5)

        # Loot Items Table
        loot_card = tk.Frame(self.tab_main, bg=self.card_bg, relief="flat", borderwidth=0)
        loot_card.pack(fill="both", expand=True)

        # Loot header
        loot_header = tk.Frame(loot_card, bg=self.card_bg)
        loot_header.pack(fill="x", padx=20, pady=(15, 10))
        tk.Label(loot_header, text="📦 Collected Items", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 11, "bold")).pack(side="left")

        # Add this Search Box next to the header
        self.loot_search_var = tk.StringVar()
        self.loot_search_var.trace_add("write", lambda *args: self.refresh_last_stats())

        search_entry = tk.Entry(loot_header, textvariable=self.loot_search_var, bg="#2d3748",
                                fg=self.text_color, insertbackground=self.text_color,
                                relief="flat", width=25)
        search_entry.pack(side="right", padx=5)
        tk.Label(loot_header, text="🔍", bg=self.card_bg, fg=self.muted_text).pack(side="right")

        # Treeview with custom styling
        tree_container = tk.Frame(loot_card, bg=self.card_bg)
        tree_container.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        self.tree = ttk.Treeview(tree_container, columns=("star", "item", "qty", "hr", "val"), show="headings")
        for col, txt in zip(self.tree["columns"], ("⭐", "Item Name", "Quantity", "Qty/h", "Total Value")):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=40 if col=="star" else 120, anchor="center" if col!="item" else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_tree_click)

        # Scrollbars
        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Grid layout for tree and scrollbars
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        # Status
        self.status_var = tk.StringVar(value="Ready")
        self.status_bar = tk.Label(self, textvariable=self.status_var, bd=1, relief="sunken",
                                  anchor="w", bg="#1e293b", fg=self.muted_text,
                                  font=("Segoe UI", 9), padx=10, pady=2)
        self.status_bar.pack(side="bottom", fill="x")

        # --- TAB 2: SETTINGS ---

        self.tab_settings = tk.Frame(self.notebook, bg=self.bg_color, relief="flat", borderwidth=0)
        self.notebook.add(self.tab_settings, text=" ⚙️ Settings ")

        # Controls content
        settings_content = tk.Frame(self.tab_settings, bg=self.card_bg)
        settings_content.pack(fill="x", padx=20, pady=(0, 20))

        settings_header = tk.Frame(settings_content, bg=self.card_bg, pady=10)
        settings_header.pack(fill="x", padx=20, pady=(15, 5))

        # Title on the left
        tk.Label(settings_header, text="⚙️ Settings", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 11, "bold")).pack(side="left")

        # Log selection on the right
        # We wrap these in a sub-frame to keep them grouped together on the right
        log_sel_frame = tk.Frame(settings_header, bg=self.card_bg)
        log_sel_frame.pack(side="right")

        ttk.Button(log_sel_frame, text="Browse", command=self.browse_file,
                  style="Secondary.TButton").pack(side="right", padx=(5, 0))

        self.log_path_var = tk.StringVar(value="info_chat_loot.log")
        log_entry = tk.Entry(log_sel_frame, textvariable=self.log_path_var, bg="#2d3748", fg=self.text_color,
                           insertbackground=self.text_color, font=("Segoe UI", 9),
                           relief="flat", width=45) # Reduced width to fit header
        log_entry.pack(side="right", padx=5)

        tk.Label(log_sel_frame, text="Log File:", bg=self.card_bg, fg=self.muted_text,
                font=("Segoe UI", 9)).pack(side="right")

        # Settings controls
        settings_controls = tk.Frame(settings_content, bg=self.card_bg)
        settings_controls.pack(fill="x", pady=8)

        tk.Label(settings_controls, text="Window:", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 10)).pack(side="left")
        self.window_minutes_var = tk.IntVar(value=120)
        window_spin = tk.Spinbox(settings_controls, from_=1, to=600, textvariable=self.window_minutes_var,
                                bg="#2d3748", fg=self.text_color, insertbackground=self.text_color,
                                font=("Segoe UI", 10), relief="flat", width=8)
        window_spin.pack(side="left", padx=(10, 20))

        tk.Label(settings_controls, text="minutes", bg=self.card_bg, fg=self.muted_text,
                font=("Segoe UI", 9)).pack(side="left")

        tk.Label(settings_controls, text="Refresh:", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 10)).pack(side="left", padx=(20, 0))
        self.refresh_secs_var = tk.IntVar(value=3)
        refresh_spin = tk.Spinbox(settings_controls, from_=1, to=60, textvariable=self.refresh_secs_var,
                                 bg="#2d3748", fg=self.text_color, insertbackground=self.text_color,
                                 font=("Segoe UI", 10), relief="flat", width=8)
        refresh_spin.pack(side="left", padx=(10, 20))

        tk.Label(settings_controls, text="seconds", bg=self.card_bg, fg=self.muted_text,
                font=("Segoe UI", 9)).pack(side="left")

        # From start checkbox
        self.from_start_var = tk.BooleanVar(value=False)
        from_start_check = tk.Checkbutton(settings_controls, text="Read from beginning",
                                         variable=self.from_start_var,
                                         bg=self.card_bg, fg=self.text_color,
                                         selectcolor=self.card_bg,
                                         activebackground=self.card_bg,
                                         activeforeground=self.text_color,
                                         font=("Segoe UI", 10))
        from_start_check.pack(side="left", padx=(20, 0))

        # --- TAB 3: MARKET PRICES ---
        self.tab_market = tk.Frame(self.notebook, bg=self.bg_color)
        self.notebook.add(self.tab_market, text=" 🏷️ Market Prices ")

        # Header & Search
        header = tk.Frame(self.tab_market, bg=self.card_bg, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="Market Price Editor", bg=self.card_bg, fg=self.accent_color, font=("Segoe UI", 12, "bold")).pack()

        search_frame = tk.Frame(self.tab_market, bg=self.bg_color, pady=10)
        search_frame.pack(fill="x", padx=20)
        tk.Label(search_frame, text="🔍 Search:", bg=self.bg_color, fg=self.text_color).pack(side="left")
        self.price_search_var = tk.StringVar()
        self.price_search_var.trace_add("write", lambda *a: self.render_price_list(self.price_search_var.get()))
        tk.Entry(search_frame, textvariable=self.price_search_var, bg="#2d3748", fg="white", relief="flat").pack(side="left", fill="x", expand=True, padx=10)

        # Scrollable View
        container = tk.Frame(self.tab_market, bg="#2d3748", bd=1, relief="flat")
        container.pack(fill="both", expand=True, padx=20, pady=10)

        self.price_canvas = tk.Canvas(container, bg=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.price_canvas.yview)
        self.scrollable_frame = tk.Frame(self.price_canvas, bg=self.bg_color)

        # Fix Scroll: Bind mousewheel ONLY when mouse is over the price list
        def _on_mousewheel(event):
            self.price_canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        self.price_canvas.bind("<Enter>", lambda e: self.price_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.price_canvas.bind("<Leave>", lambda e: self.price_canvas.unbind_all("<MouseWheel>"))

        self.price_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw", width=400)
        self.price_canvas.configure(yscrollcommand=scrollbar.set)
        self.price_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Initialize the list
        self.row_widgets = []
        self.render_price_list()

    def render_price_list(self, filter_text=""):
        # Safety check for initialization
        if not hasattr(self, 'scrollable_frame') or not hasattr(self, 'row_widgets'):
            self.row_widgets = []
            return

        # 1. Clear current rows
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.row_widgets.clear()

        # 2. Render filtered items
        for name, price in self.price_db.items():
            if filter_text.lower() in name.lower():
                f = tk.Frame(self.scrollable_frame, bg=self.bg_color, pady=5)
                f.pack(fill="x", padx=5)

                tk.Label(f, text=name, bg=self.bg_color, fg=self.text_color,
                         width=25, anchor="w", font=("Segoe UI", 10)).pack(side="left")

                var = tk.StringVar(value=str(price))
                var.trace_add("write", lambda *args, n=name, v=var: self.on_price_change(n, v))

                e = tk.Entry(f, textvariable=var, bg="#16213e", fg=self.accent_secondary,
                             width=12, justify="center", relief="flat", font=("Segoe UI", 10, "bold"))
                e.pack(side="right", padx=5)

                self.row_widgets.append((name, var))

        # 3. CRITICAL FIX FOR SEARCH: Update canvas scroll area
        self.scrollable_frame.update_idletasks()
        self.price_canvas.configure(scrollregion=self.price_canvas.bbox("all"))

        # Reset scroll to top when searching so results are visible
        if filter_text:
            self.price_canvas.yview_moveto(0)

    def toggle_mini_window(self):
        """Toggles the mini window on and off."""
        # If window exists, close it and clean up
        if hasattr(self, 'mini_win') and self.mini_win is not None and self.mini_win.winfo_exists():
            self.mini_win.destroy()
            self.mini_win = None
            return

        # Otherwise, create it
        self.mini_win = tk.Toplevel(self)
        self.mini_win.title("Loot Mini")
        self.mini_win.geometry("220x100")
        self.mini_win.attributes("-topmost", True)
        self.mini_win.configure(bg="#1a1a2e")
        self.mini_win.overrideredirect(True) # Borderless

        # Draggable logic
        def start_move(event): self.mini_win.x, self.mini_win.y = event.x, event.y
        def on_move(event):
            deltax = event.x - self.mini_win.x
            deltay = event.y - self.mini_win.y
            self.mini_win.geometry(f"+{self.mini_win.winfo_x() + deltax}+{self.mini_win.winfo_y() + deltay}")

        self.mini_win.bind("<Button-1>", start_move)
        self.mini_win.bind("<B1-Motion>", on_move)

        # UI Elements
        self.mini_yang = tk.Label(self.mini_win, text="Yang: 0", fg=self.accent_color, bg="#1a1a2e", font=("Segoe UI", 11, "bold"))
        self.mini_yang.pack(pady=(15, 2))

        self.mini_hr = tk.Label(self.mini_win, text="Yang/h: 0", fg=self.accent_secondary, bg="#1a1a2e", font=("Segoe UI", 10))
        self.mini_hr.pack()

        self.mini_min = tk.Label(self.mini_win, text="Yang/m: 0", fg="#f59e0b", bg="#1a1a2e", font=("Segoe UI", 10))
        self.mini_min.pack()

    def create_dungeon_block(self, parent, name, count, bg):
        """Creates a styled card for a dungeon run."""
        frame = tk.Frame(parent, bg=bg, padx=12, pady=8, highlightthickness=1, highlightbackground="#374151")
        tk.Label(frame, text=name.upper(), bg=bg, fg=self.text_color, font=("Segoe UI", 8, "bold")).pack()
        tk.Label(frame, text=str(count), bg=bg, fg="white", font=("Segoe UI", 14, "bold")).pack()
        return frame

    def render_dungeon_blocks(self, stats):
        # Clear old blocks
        for widget in self.dungeon_blocks.winfo_children():
            widget.destroy()

        dungeon_data = stats.get("dungeon_runs", {})
        total_runs = stats.get("total_dungeon_runs", 0)

        if total_runs == 0:
            self.create_dungeon_block(self.dungeon_blocks, "No Runs", 0, self.card_bg).pack(side="left", padx=5)
        else:
            # Render Total Block
            self.create_dungeon_block(self.dungeon_blocks, "Total", total_runs, "#374151").pack(side="left", padx=5)

            # Render Individual Dungeons (Sorted by name)
            for name in DUNGEONS.keys():
              if name in dungeon_data:
                  color = DUNGEONS[name]["color"]
                  count = dungeon_data[name]
                  self.create_dungeon_block(self.dungeon_blocks, name, count, color).pack(side="left", padx=2)

    # -------------------- UI helpers -------------------- #

    def reset_stats_ui(self):
        """Clear stats and item list for a fresh start."""
        self.interval_label.config(text="Not started", fg=self.muted_text)
        self.window_length_label.config(text="0.00 h", fg=self.muted_text)
        self.yang_label.config(text="0", fg=self.accent_color)
        self.yang_per_hour_label.config(text="0", fg=self.accent_secondary)
        self.yang_per_minute_label.config(text="0", fg="#f59e0b")
        self.material_value_label.config(text="0", fg="#a855f7")
        self.tree.delete(*self.tree.get_children())

    def update_status(self, text: str):
        """Updates the text in the status bar."""
        self.status_var.set(text)

    def load_prices(self):
      self.price_db = {}
      path = "prices.json"
      if os.path.exists(path):
          try:
              with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                  self.price_db = data
          except (json.JSONDecodeError, OSError):
                self.price_db = {}
      else:
          # Default example file
          self.price_db = {"Shard": 1000}
          with open(path, "w", encoding="utf-8") as f:
              json.dump(self.price_db, f, indent=4)

    def on_price_change(self, name, var):
        """Update database and file immediately on keystroke."""
        try:
            new_val = int(var.get())
            self.price_db[name] = new_val
            # Update the worker's DB so the next calculation uses the new price
            if self.worker:
                self.worker.price_db = self.price_db
            self.save_data()
        except ValueError:
            pass # Ignore non-numeric input during typing

    def save_data(self):
        with open("prices.json", "w") as f: json.dump(self.price_db, f)
        with open("bookmarks.json", "w") as f: json.dump(list(self.bookmarks), f)

    def refresh_treeview_filtered(self):
        if not self.last_received_stats:
            return

        search_query = self.loot_search_var.get().lower().strip()
        self.tree.delete(*self.tree.get_children())

        items_list = self.last_received_stats.get("items", [])

        # Sort items: Pinned vs Others
        pinned_items = [i for i in items_list if i[0] in self.bookmarks]
        other_items = [i for i in items_list if i[0] not in self.bookmarks]

        def insert_batch(batch, is_pinned):
            for name, qty, per_hour, val in batch:
                if not search_query or search_query in name.lower():
                    # Solid star for pinned, hollow for not
                    icon = "⭐" if is_pinned else "☆"

                    # Tagging for colors
                    tag = 'pinnedrow' if is_pinned else 'normalrow'

                    self.tree.insert("", "end", values=(
                        icon, name, f"{qty:,}", f"{per_hour:,}", f"{val:,}"
                    ), tags=(tag,))

        insert_batch(pinned_items, True)
        insert_batch(other_items, False)

        # Make the default icons a bit more muted
        self.tree.tag_configure('normalrow', foreground=self.text_color)
        self.tree.tag_configure('pinnedrow', background='#2d2d1a', foreground="#fbbf24")

    def refresh_last_stats(self):
      """Helper for the search trace to update the UI immediately."""
      self.refresh_treeview_filtered()

    def load_bookmarks(self):
      """Load bookmarked item names from bookmarks.json."""
      self.bookmarks = set()
      path = "bookmarks.json"
      if os.path.exists(path):
          try:
              with open(path, "r", encoding="utf-8") as f:
                  data = json.load(f)
                  self.bookmarks = set(data)
          except Exception as e:
              print(f"Error loading bookmarks: {e}")

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)

        if not item_id:
            return

        # Check if the click was in the first column (#1 is the Bookmark column)
        if column == "#1":
            item_values = self.tree.item(item_id, "values")
            item_name = item_values[1] # Name is in the second column

            if item_name in self.bookmarks:
                self.bookmarks.remove(item_name)
            else:
                self.bookmarks.add(item_name)

            self.save_data()
            self.refresh_last_stats()

    def format_yang_short(self, amount):
        # Handle zero or negative values to avoid math domain errors
        if amount <= 0:
            return "0"

        # Your custom suffixes
        suffixes = ["", "k", "kk", "kkk", "kkkk"]

        # Calculate the magnitude (index)
        # math.log10(amount) / 3 tells us how many groups of 3 zeros there are
        i = int(math.floor(math.log10(amount) / 3))

        # Ensure the index doesn't exceed the available suffixes
        i = min(i, len(suffixes) - 1)

        if i == 0:
            return str(amount)

        # Calculate the shortened value
        value = amount / (1000 ** i)

        # Format to 1 decimal place and strip ".0" if it's a whole number
        return "{:.1f}".format(value).rstrip('0').rstrip('.') + suffixes[i]

    # -------------------- UI callbacks -------------------- #

    def browse_file(self):
        filename = filedialog.askopenfilename(
            title="Select log file", filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")]
        )
        if filename:
            self.log_path_var.set(filename)

    def start_monitor(self):
        if self.worker is not None:
            messagebox.showinfo("Info", "Monitor is already running.")
            return

        path = self.log_path_var.get().strip()
        if not path:
            messagebox.showerror("Error", "Please select a log file.")
            return

        # Wipe UI data and start fresh
        self.reset_stats_ui()

        window_minutes = self.window_minutes_var.get()
        refresh_secs = self.refresh_secs_var.get()
        from_start = self.from_start_var.get()

        self.stop_event = threading.Event()
        self.worker = LiveMonitorWorker(
            path=path,
            window_minutes=window_minutes,
            refresh_secs=refresh_secs,
            from_start=from_start,
            update_callback=self.schedule_update_stats,
            stop_event=self.stop_event,
        )
        self.worker.price_db = self.price_db
        self.worker.start()

        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")

    def stop_monitor(self):
        if self.worker is not None:
            self.stop_event.set()
            # Ensure worker has time to close the file
            self.worker.join(timeout=1.0)
            self.worker = None

        # Keep the data in the UI, just re-enable Start
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")

    def on_close(self):
        self.stop_monitor()
        self.destroy()

    # -------------------- Stats update -------------------- #

    def schedule_update_stats(self, stats: Dict):
        """
        Called from the worker thread.
        We must schedule the actual UI update on the Tkinter main thread using after().
        """
        self.after(0, self.update_stats, stats)

    def update_stats(self, stats: Dict):
        self.last_received_stats = stats
        self.refresh_treeview_filtered()

        if "status" in stats:
            self.update_status(stats["status"])
            if len(stats) == 1: # If it's only a status update, stop here
                return

        if "error" in stats:
            messagebox.showerror("Error", stats["error"])
            self.update_status("Error occurred.")
            self.stop_monitor()
            return

        start = stats["start"]
        end = stats["end"]
        hours = stats["hours"]
        minutes = stats["minutes"]
        dropped_yang = stats["dropped_yang"]
        yang_per_hour = stats["yang_per_hour"]
        yang_per_minute = stats["yang_per_minute"]
        items_list = stats["items"]

        if hasattr(self, 'mini_win') and self.mini_win is not None and self.mini_win.winfo_exists():
            try:
                self.mini_yang.config(text=f"Yang: {dropped_yang:,} ({self.format_yang_short(dropped_yang)})")
                self.mini_hr.config(text=f"Yang/h: {yang_per_hour:,} ({self.format_yang_short(yang_per_hour)})")
                self.mini_min.config(text=f"Yang/m: {yang_per_minute:,} ({self.format_yang_short(yang_per_minute)})")
            except Exception:
                # This catches cases where winfo_exists was true but the widget was mid-destruction
                pass

        # Update time info
        if start.date() != end.date():
            # Shows: 24/11 22:30:05 -> 25/11 01:15:00
            time_str = f"{start.strftime('%d/%m %H:%M:%S')} → {end.strftime('%d/%m %H:%M:%S')}"
        else:
            # Standard view for single-day sessions
            time_str = f"{start.strftime('%H:%M:%S')} → {end.strftime('%H:%M:%S')}"

        self.interval_label.config(
            text=time_str,
            fg=self.text_color
        )

        if hours >= 24:
            days = int(hours // 24)
            rem_hours = hours % 24
            window_txt = f"{days} Days {rem_hours:.1f} Hours ({stats['minutes']:.1f} Minutes)"
        else:
            window_txt = f"{hours:.2f} Hours ({stats['minutes']:.1f} Minutes)"

        self.window_length_label.config(text=window_txt, fg=self.text_color)

        # Calculate Material Value
        material_value = sum(item[3] for item in stats["items"])

        # Update yang stats
        self.yang_label.config(text=f"{dropped_yang:,} ({self.format_yang_short(dropped_yang)}) Yang")
        self.yang_per_hour_label.config(text=f"{yang_per_hour:,} ({self.format_yang_short(yang_per_hour)}) Yang")
        self.yang_per_minute_label.config(text=f"{yang_per_minute:,} ({self.format_yang_short(yang_per_minute)}) Yang")
        self.material_value_label.config(text=f"{material_value:,} ({self.format_yang_short(material_value)}) Yang")

        # Render the dynamic Dungeon blocks
        self.render_dungeon_blocks(stats)

        # Update items tree
        self.tree.delete(*self.tree.get_children())
        for idx, (name, qty, per_hour, val ) in enumerate(items_list):
            # Alternate row colors
            tag = 'evenrow' if idx % 2 == 0 else 'oddrow'

            self.tree.insert(
                "",
                "end",
                values=(
                    name,
                    f"{qty:,}",
                    f"{per_hour:,}",
                    f"{val:,}" # Show the total value of that stack
                ),
                tags=(tag,)
            )

        # Configure row colors
        self.tree.tag_configure('evenrow', background='#2d3748', foreground=self.text_color)
        self.tree.tag_configure('oddrow', background='#374151', foreground=self.text_color)

        # Check for new items to add to price DB
        new_item = False
        for item in stats['items']:
            if item[0] not in self.price_db:
                self.price_db[item[0]] = 0
                new_item = True
        if new_item:
            self.render_price_list()
            self.save_data()

        self.refresh_treeview_filtered()

def main():
    app = LootMonitorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
