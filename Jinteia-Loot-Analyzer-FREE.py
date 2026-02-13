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

# ---------------------------------------------------------------------------
# Parsing and data structures
# ---------------------------------------------------------------------------

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
        for ev in events_list:
            if not ev.is_yang:
                items_qty[ev.item] += ev.quantity

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

        self.title("💰 Jinteia Loot Analyzer [AI Slop by Paysami] - Real-time Yang & Loot Tracker - Download only from there: https://github.com/PaysamiKekW/Jinteia-Loot-Analyzer-FREE/")
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
        # Create a main container with padding
        main_container = ttk.Frame(self)
        main_container.pack(fill="both", expand=True, padx=10, pady=10)

        # Settings Card
        settings_card = tk.Frame(main_container, bg=self.card_bg, relief="flat", borderwidth=0)
        settings_card.pack(fill="x", pady=(0, 5))

        # --- Updated Settings Header with Log Selection ---
        settings_header = tk.Frame(settings_card, bg=self.card_bg)
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
        # Settings content
        settings_content = tk.Frame(settings_card, bg=self.card_bg)
        settings_content.pack(fill="x", padx=20, pady=(0, 20))

        # Row 2: Settings controls
        row2 = tk.Frame(settings_content, bg=self.card_bg)
        row2.pack(fill="x", pady=8)

        tk.Label(row2, text="Window:", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 10)).pack(side="left")
        self.window_minutes_var = tk.IntVar(value=120)
        window_spin = tk.Spinbox(row2, from_=1, to=600, textvariable=self.window_minutes_var,
                                bg="#2d3748", fg=self.text_color, insertbackground=self.text_color,
                                font=("Segoe UI", 10), relief="flat", width=8)
        window_spin.pack(side="left", padx=(10, 20))

        tk.Label(row2, text="minutes", bg=self.card_bg, fg=self.muted_text,
                font=("Segoe UI", 9)).pack(side="left")

        tk.Label(row2, text="Refresh:", bg=self.card_bg, fg=self.text_color,
                font=("Segoe UI", 10)).pack(side="left", padx=(20, 0))
        self.refresh_secs_var = tk.IntVar(value=2)
        refresh_spin = tk.Spinbox(row2, from_=1, to=60, textvariable=self.refresh_secs_var,
                                 bg="#2d3748", fg=self.text_color, insertbackground=self.text_color,
                                 font=("Segoe UI", 10), relief="flat", width=8)
        refresh_spin.pack(side="left", padx=(10, 20))

        tk.Label(row2, text="seconds", bg=self.card_bg, fg=self.muted_text,
                font=("Segoe UI", 9)).pack(side="left")

        # From start checkbox
        self.from_start_var = tk.BooleanVar(value=False)
        from_start_check = tk.Checkbutton(row2, text="Read from beginning",
                                         variable=self.from_start_var,
                                         bg=self.card_bg, fg=self.text_color,
                                         selectcolor=self.card_bg,
                                         activebackground=self.card_bg,
                                         activeforeground=self.text_color,
                                         font=("Segoe UI", 10))
        from_start_check.pack(side="left", padx=(20, 0))

        # Row 3: Control buttons
        row3 = tk.Frame(settings_content, bg=self.card_bg)
        row3.pack(fill="x", pady=(15, 0))

        self.start_button = ttk.Button(row3, text="▶ Start Monitoring",
                                      command=self.start_monitor, style="Accent.TButton")
        self.start_button.pack(side="left", padx=(0, 10))

        self.stop_button = ttk.Button(row3, text="⏹ Stop",
                                     command=self.stop_monitor, style="Secondary.TButton",
                                     state="disabled")
        self.stop_button.pack(side="left")

        self.price_button = ttk.Button(row3, text="🏷️ Edit Prices",
                                     command=self.open_price_editor, style="Secondary.TButton")
        self.price_button.pack(side="left", padx=10)

        self.mini_btn = ttk.Button(row3, text="📱 Mini Mode",
                                  command=self.toggle_mini_window, style="Secondary.TButton")
        self.mini_btn.pack(side="left", padx=10)

        # Stats Dashboard
        stats_card = tk.Frame(main_container, bg=self.card_bg, relief="flat", borderwidth=0)
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

        # Loot Items Table
        loot_card = tk.Frame(main_container, bg=self.card_bg, relief="flat", borderwidth=0)
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

        columns = ("bookmark", "item", "quantity", "per_hour", "total")
        self.tree = ttk.Treeview(tree_container, columns=columns, show="headings", height=8)

        # Configure columns
        self.tree.heading("bookmark", text="⭐", anchor="center")
        self.tree.heading("item", text="Item Name", anchor="w")
        self.tree.heading("quantity", text="Quantity", anchor="center")
        self.tree.heading("per_hour", text="Quantity / Hour", anchor="center")
        self.tree.heading("total", text="Total", anchor="center")

        self.tree.column("bookmark", width=40, anchor="center")
        self.tree.column("item", width=150, anchor="w")
        self.tree.column("quantity", width=100, anchor="center")
        self.tree.column("per_hour", width=100, anchor="center")
        self.tree.column("total", width=150, anchor="center")

        self.tree.bind("<Button-1>", self.on_click_handler)

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

    def open_price_editor(self):
        editor = tk.Toplevel(self)
        editor.title("🏷️ Price Database")
        editor.geometry("450x600")
        editor.configure(bg=self.bg_color)
        editor.transient(self)
        editor.grab_set() # Forces focus on this window

        # --- Header ---
        header = tk.Frame(editor, bg=self.card_bg, pady=15)
        header.pack(fill="x")
        tk.Label(header, text="Market Price Editor", bg=self.card_bg, fg=self.accent_color,
                font=("Segoe UI", 14, "bold")).pack()
        tk.Label(header, text="Changes apply to Net Worth calculations",
                bg=self.card_bg, fg=self.muted_text, font=("Segoe UI", 9)).pack()

        # --- Search Box ---
        search_frame = tk.Frame(editor, bg=self.bg_color, pady=10)
        search_frame.pack(fill="x", padx=20)
        tk.Label(search_frame, text="🔍 Search:", bg=self.bg_color, fg=self.text_color).pack(side="left")
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=search_var, bg="#2d3748",
                                fg="white", insertbackground="white", relief="flat")
        search_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # --- Scrollable Area Container ---
        # We use a frame with a specific height to act as the viewport
        container = tk.Frame(editor, bg="#2d3748", bd=1, relief="flat")
        container.pack(fill="both", expand=True, padx=20, pady=10)

        canvas = tk.Canvas(container, bg=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=self.bg_color)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw", width=380)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mousewheel support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        row_widgets = []

        def render_list(filter_text=""):
            # Clear current rows
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            row_widgets.clear()

            for name, price in self.price_db.items():
                if filter_text.lower() in name.lower():
                    f = tk.Frame(scrollable_frame, bg=self.bg_color, pady=5)
                    f.pack(fill="x", padx=5)

                    # Item Name Label
                    tk.Label(f, text=name, bg=self.bg_color, fg=self.text_color,
                            font=("Segoe UI", 10), width=25, anchor="w").pack(side="left")

                    # Price Entry
                    var = tk.StringVar(value=str(price))
                    e = tk.Entry(f, textvariable=var, bg="#16213e", fg=self.accent_secondary,
                                font=("Segoe UI", 10, "bold"), width=12, relief="flat", justify="center")
                    e.pack(side="right", padx=5)
                    row_widgets.append((name, var))

        # Bind search logic
        search_var.trace_add("write", lambda *args: render_list(search_var.get()))

        # Initial render
        render_list()

        # --- Footer Action ---
        def save_and_close():
            # Update local db from all variables (even hidden ones)
            # Note: In a real search scenario, we'd store all vars in a master dict
            for name, var in row_widgets:
                try:
                    self.price_db[name] = int(var.get())
                except: continue

            with open("prices.json", "w", encoding="utf-8") as f:
                json.dump(self.price_db, f, indent=4)

            self.update_status("Prices updated successfully.")
            canvas.unbind_all("<MouseWheel>") # Clean up binding
            editor.destroy()

        btn_frame = tk.Frame(editor, bg=self.bg_color, pady=15)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="✅ Save & Apply", style="Accent.TButton",
                  command=save_and_close).pack()

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

    def save_bookmarks(self):
        """Save the current set of bookmarks to bookmarks.json."""
        try:
            with open("bookmarks.json", "w", encoding="utf-8") as f:
                # Convert set to list for JSON compatibility
                json.dump(list(self.bookmarks), f, indent=4)
        except Exception as e:
            print(f"Error saving bookmarks: {e}")

    def on_click_handler(self, event):
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

            self.save_bookmarks()
            self.refresh_last_stats()

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

        if hasattr(self, 'mini_win') and self.mini_win is not None and self.mini_win.winfo_exists():
            try:
                self.mini_yang.config(text=f"Yang: {dropped_yang:,}")
                self.mini_hr.config(text=f"Hr: {yang_per_hour:,}")
                self.mini_min.config(text=f"Min: {yang_per_minute:,}")
            except Exception:
                # This catches cases where winfo_exists was true but the widget was mid-destruction
                pass

        start = stats["start"]
        end = stats["end"]
        hours = stats["hours"]
        minutes = stats["minutes"]
        dropped_yang = stats["dropped_yang"]
        yang_per_hour = stats["yang_per_hour"]
        yang_per_minute = stats["yang_per_minute"]
        items_list = stats["items"]

        # Update time info
        self.interval_label.config(
            text=f"{start.strftime('%H:%M:%S')} → {end.strftime('%H:%M:%S')}",
            fg=self.text_color
        )
        self.window_length_label.config(
            text=f"{hours:.2f} h ({minutes:.1f} min)",
            fg=self.text_color
        )

        # Calculate Material Value
        material_value = sum(item[3] for item in stats["items"])

        # Update yang stats
        self.yang_label.config(text=f"{dropped_yang:,} Yang")
        self.yang_per_hour_label.config(text=f"{yang_per_hour:,} Yang")
        self.yang_per_minute_label.config(text=f"{yang_per_minute:,} Yang")
        self.material_value_label.config(text=f"{material_value:,} Yang")

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

        # Check for items not in our price database
        prices_changed = False
        for item_tuple in stats["items"]:
            item_name = item_tuple[0]
            if item_name not in self.price_db:
                self.price_db[item_name] = 0 # Default price for new item
                prices_changed = True

        if prices_changed:
            # Save the new items to the JSON file automatically
            with open("prices.json", "w", encoding="utf-8") as f:
                json.dump(self.price_db, f, indent=4)

        self.refresh_treeview_filtered()

def main():
    app = LootMonitorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
