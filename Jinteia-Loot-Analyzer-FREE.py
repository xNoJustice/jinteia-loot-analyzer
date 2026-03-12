#!/usr/bin/env python3
import datetime as dt
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional, List, Deque, Dict, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import math
import pygame

CURRENT_VERSION = "1.3"
VERSION_URL = "https://raw.githubusercontent.com/xNoJustice/jinteia-loot-analyzer/main/version.txt"
DOWNLOAD_URL = "https://github.com/xNoJustice/jinteia-loot-analyzer" # Link to your GitHub

# ---------------------------------------------------------------------------
# PARSING AND DATA STRUCTURES: CORE UTILITIES FOR LOG PROCESSING
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

# PERMISSIVE REGEX: SEARCHES FOR THE PATTERN ANYWHERE IN THE LINE
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
    
# ---------------------------------------------------------------------------
# LIVE MONITOR WORKER: ASYNCHRONOUS THREAD FOR REAL-TIME LOG TAILING
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
        new_event_callback=None,
    ):
        super().__init__(daemon=True)
        self.path = path
        self.window_minutes = window_minutes
        self.refresh_secs = refresh_secs
        self.from_start = from_start
        self.update_callback = update_callback
        self.stop_event = stop_event
        self.new_event_callback = new_event_callback

        self.window: Deque[LootEvent] = deque()
        self.processed_count = 0
        self.last_item_name = "None"

    def add_event(self, ev: LootEvent, ignore_cutoff: bool = False):
        self.window.append(ev)
        self.processed_count += 1
        self.last_item_name = ev.item
        
        if not ignore_cutoff:
            cutoff = ev.ts - dt.timedelta(minutes=self.window_minutes)
            while self.window and self.window[0].ts < cutoff:
                self.window.popleft()

    def compute_stats_from_window(self) -> Optional[Dict]:
        if not self.window:
            return None

        # SORT WINDOW ONCE BEFORE CALCULATING
        events_list = sorted(list(self.window), key=lambda x: x.ts)
        if not events_list:
            return None

        # CALCULATE SESSION TIME BASELINE (CRITICAL FIX FOR NAMEERRORS)
        start   = events_list[0].ts
        end     = events_list[-1].ts
        elapsed = max((end - start).total_seconds(), 1)
        hours   = elapsed / 3600.0
        minutes = elapsed / 60.0
        
        dropped_yang = sum(ev.quantity for ev in events_list if ev.is_yang)
        items_qty = defaultdict(int)
        dungeon_runs = defaultdict(int)
        total_runs = 0

        for ev in events_list:
            if not ev.is_yang:
                items_qty[ev.item] += ev.quantity
                
                # DUNGEON TRACKING (CASE-SENSITIVE MATCHING GAME LOGS)
                for d_name, d_data in DUNGEONS.items():
                    if ev.item in d_data["items"]:
                        dungeon_runs[d_name] += ev.quantity
                        total_runs += ev.quantity

        # BUILD THE FINAL LIST FOR UI
        items_list: List[Tuple[str, int, int, int]] = []
        prices = getattr(self, 'price_db', {})
        for name, qty in items_qty.items():
            per_hour = int(round(qty / hours))
            price = prices.get(name, 0)
            item_value = qty * price
            items_list.append((name, qty, per_hour, item_value))

        # ORDER THE COLLECTED ITEMS BY THEIR TOTAL QUANTITY IN DESCENDING ORDER
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
            # NOTIFY THE MAIN UI THREAD ABOUT FILE SYSTEM ERRORS VIA CALLBACK
            self.update_callback({"error": f"Cannot open log file: {e}"})
            return

        if self.from_start:
            self.update_callback({"status": "Reading historical data (this may take a moment)..."})
            # PROCESS THE ENTIRE BACKLOG OF THE CURRENT LOG FILE ON INITIALIZATION
            for line in f:
                ev = parse_log_line(line)
                if ev:
                    # ENABLE RETENTION OF OLD LOG DATA DURING THE STARTUP SCANNING PHASE
                    self.add_event(ev, ignore_cutoff=True)


            self.update_callback({"status": "Historical data loaded. Starting live monitor."})
            # REFRESH THE DASHBOARD COMPONENTS INSTANTLY ONCE THE BACKLOG IS PROCESSED
            stats = self.compute_stats_from_window()
            if stats:
                self.update_callback(stats)
        else:
            # SEEK TO THE END OF THE FILE TO IGNORE HISTORICAL ENTRIES AND ONLY CAPTURE NEW DATA
            f.seek(0, os.SEEK_END)
            self.update_callback({"status": "Monitoring live logs..."})

        last_print = time.time()

        while not self.stop_event.is_set():
            lines_found = False
            while True:
                line = f.readline()
                if not line:
                    break

                ev = parse_log_line(line)
                if ev:
                    self.add_event(ev)
                    lines_found = True
                    if self.new_event_callback:
                        self.new_event_callback(ev)

            if not lines_found:
                time.sleep(0.2)
               
            now_ts = time.time()
            if now_ts - last_print >= self.refresh_secs:
                last_print = now_ts
                stats = self.compute_stats_from_window()
                if stats is not None:
                    stats["processed_count"] = self.processed_count
                    stats["last_item"] = self.last_item_name
                    self.update_callback(stats)

        f.close()
        self.update_callback({"status": "Monitoring stopped."})

# ---------------------------------------------------------------------------
# TKINTER USER INTERFACE: MAIN APPLICATION FRAMEWORK AND VISUAL COMPONENTS
# ---------------------------------------------------------------------------

class LootMonitorApp(tk.Tk):
    # ── INITIALIZATION: SETUP CORE APP STATE AND THEME ──────────────────
    def __init__(self):
        super().__init__()
        self.title("⚔️ Jinteia Loot Analyzer")
        self.geometry("700x900")

        # colour palette
        self.bg_color         = "#0d0f14"
        self.sidebar_bg       = "#111318"
        self.header_bg        = "#111318"
        self.card_bg          = "#181b24"
        self.card_border      = "#252a38"
        self.input_bg         = "#2a2e3f"
        self.input_border     = "#454d66"
        self.accent_color     = "#00c853"
        self.accent_hover     = "#00e676"
        self.accent_secondary = "#00b4d8"
        self.accent_amber     = "#f59e0b"
        self.text_color       = "#e0e6f0"
        self.muted_text       = "#4e5a72"
        self.sep_color        = "#1e2234"

        self.configure(bg=self.bg_color)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._apply_styles()

        self.stop_event          = threading.Event()
        self.worker: Optional[LiveMonitorWorker] = None
        self.last_received_stats = None
        self.glow_val = 255
        self.glow_dir = -8

        # INITIALIZE THE PYGAME MIXER FOR MULTIMEDIA FEEDBACK AND PERSISTENT AUDIO RULES
        try:
            pygame.mixer.init()
        except: pass
        self.drop_sounds = []
        self.drop_sounds_enabled = tk.BooleanVar(value=True)
        self.default_sound_duration = tk.DoubleVar(value=5.0)
        self.mini_pos = None # PERSISTENT COORDINATE STRING FOR MINI-MODE WINDOW POSITIONING

        self.load_bookmarks()
        self.load_prices()
        self.load_sounds()
        self.create_widgets()
        self.load_data()
        self.after(2000, self.check_for_updates_ui)

    # ── STYLING: DEFINE CUSTOM TTK LOOK AND FEEL ────────────────────────
    def _apply_styles(self):
        self.style.configure("Treeview",
            background=self.card_bg, foreground=self.text_color,
            fieldbackground=self.card_bg, borderwidth=0,
            font=("Segoe UI", 10), rowheight=26)
        self.style.configure("Treeview.Heading",
            background=self.input_bg, foreground=self.accent_secondary,
            font=("Segoe UI", 10, "bold"), relief="flat", borderwidth=0)
        self.style.map("Treeview",
            background=[("selected", "#2a3a5a")],
            foreground=[("selected", "white")])
        self.style.map("Treeview.Heading",
            background=[("active", self.input_bg)])

        self.style.configure("Accent.TButton",
            background=self.accent_color, foreground="#000",
            font=("Segoe UI", 10, "bold"), borderwidth=1, bordercolor=self.accent_color,
            padding=(12, 7))
        self.style.map("Accent.TButton",
            background=[("active", self.accent_hover), ("disabled", "#1a2f1a")],
            bordercolor=[("active", self.accent_hover)])

        self.style.configure("Secondary.TButton",
            background=self.input_bg, foreground=self.text_color,
            font=("Segoe UI", 10), borderwidth=1, bordercolor=self.card_border,
            padding=(10, 6))
        self.style.map("Secondary.TButton",
            background=[("active", "#2a3252")],
            bordercolor=[("active", self.accent_secondary)])

        self.style.configure("Danger.TButton",
            background="#7f1d1d", foreground=self.text_color,
            font=("Segoe UI", 10, "bold"), borderwidth=1, bordercolor="#991b1b",
            padding=(10, 6))
        self.style.map("Danger.TButton",
            background=[("active", "#991b1b")])
        self.style.configure("Vertical.TScrollbar",
            background=self.card_border, troughcolor=self.card_bg,
            borderwidth=0, arrowsize=12)

    def check_for_updates_ui(self):
        try:
            import urllib.request
            import webbrowser
            
            # Use timeout so the app doesn't freeze if internet is slow
            with urllib.request.urlopen(VERSION_URL, timeout=2) as response:
                latest_version = response.read().decode('utf-8').strip()

            if latest_version > CURRENT_VERSION:
                msg = (
                    f"A new version ({latest_version}) is available!\n"
                    f"Current version: {CURRENT_VERSION}\n\n"
                    f"Click 'Yes' to open the download page."
                )
                # We use self.root as the parent so the popup stays on top
                if messagebox.askyesno("Update Available", msg, parent=self):
                    webbrowser.open(DOWNLOAD_URL)
        except:
            pass

    # ── ROOT LAYOUT: CONSTRUCT MAIN APP CONTAINERS ──────────────────────
    def create_widgets(self):
        # status bar
        sb = tk.Frame(self, bg=self.header_bg, height=26)
        sb.pack(side="bottom", fill="x")
        sb.pack_propagate(False)
        self.status_var = tk.StringVar(value="Ready — select a log file and press Start.")
        tk.Label(sb, textvariable=self.status_var, bg=self.header_bg,
                 fg=self.text_color, font=("Segoe UI", 9), anchor="w",
                 padx=12).pack(side="left", fill="y")
        self.live_dot = tk.Label(sb, text="●", bg=self.header_bg,
                                 fg=self.text_color, font=("Segoe UI", 10))
        self.live_dot.pack(side="right", padx=(0, 12))
        tk.Label(sb, text="LIVE", bg=self.header_bg, fg=self.text_color,
                 font=("Segoe UI", 8, "bold")).pack(side="right")

        # header
        hdr = tk.Frame(self, bg=self.header_bg, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        left = tk.Frame(hdr, bg=self.header_bg)
        left.pack(side="left", padx=16, fill="y")
        tk.Label(left, text="⚔", bg=self.header_bg, fg=self.accent_color,
                 font=("Segoe UI", 18)).pack(side="left", padx=(0, 8))
        tk.Label(left, text="JINTEIA", bg=self.header_bg, fg=self.text_color,
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(left, text="  LOOT ANALYZER", bg=self.header_bg,
                 fg=self.accent_color, font=("Segoe UI", 14, "bold")).pack(side="left")

        # PRIMARY MONITORING CONTROLS POSITIONED IN THE UPPER RIGHT HEADER AREA
        ctrl_f = tk.Frame(hdr, bg=self.header_bg)
        ctrl_f.pack(side="right", padx=12, fill="y")

        def _cbtn(text, cmd, bg, fg):
            b = tk.Button(ctrl_f, text=text, bg=bg, fg=fg,
                          font=("Segoe UI", 9, "bold"), relief="flat", bd=0, 
                          cursor="hand2", padx=14, pady=4,
                          activebackground=bg, activeforeground=fg,
                          command=cmd)
            b.pack(side="left", padx=4, pady=10) # Centered vertically in the 52px header
            return b

        self.mini_window_button = _cbtn("📱 Mini", self.toggle_mini_window, "#2a3c5a", self.text_color)
        self.stop_button  = _cbtn("⏹ Stop",  self.stop_monitor,  "#7f1d1d", self.text_color)
        self.stop_button.configure(state="disabled")
        self.start_button = _cbtn("▶ Start", self.start_monitor, self.accent_color, "#000")
        
        # body
        self.topnav = tk.Frame(self, bg=self.sidebar_bg, height=44)
        self.topnav.pack(fill="x")
        self.topnav.pack_propagate(False)
        self._build_topnav(self.topnav)

        tk.Frame(self, bg=self.sep_color, height=1).pack(fill="x")

        self.content_area = tk.Frame(self, bg=self.bg_color)
        self.content_area.pack(fill="both", expand=True)

        self.pages = {}
        self._build_dashboard()
        self._build_settings()
        self._build_market()
        self._build_sounds()
        self.show_page("dashboard")

    # ── TOP NAVIGATION: GLOBAL PAGE SWITCHING AREA ──────────────────────
    def _build_topnav(self, parent):
        # Nav buttons on left
        nav_f = tk.Frame(parent, bg=self.sidebar_bg)
        nav_f.pack(side="left", padx=8)

        self.nav_btns = {}
        for key, label in [("dashboard", "🎮 Dashboard"),
                            ("settings", "⚙️ Settings"),
                            ("market", "🏷️ Market Prices"),
                            ("sounds", "🔊 Drop Sounds")]:
            btn = tk.Button(nav_f, text=label, bg=self.sidebar_bg, fg=self.text_color,
                            font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
                            padx=12, activebackground=self.sep_color,
                            activeforeground=self.accent_color,
                            command=lambda k=key: self.show_page(k))
            btn.pack(side="left", padx=2, pady=6, ipady=4)
            self.nav_btns[key] = btn

    def show_page(self, key):
        for pg in self.pages.values():
            pg.pack_forget()
        for k, b in self.nav_btns.items():
            b.configure(bg=(self.sep_color if k == key else self.sidebar_bg),
                        fg=(self.accent_color if k == key else self.text_color))
        if key == "sounds":
            self._refresh_sound_item_list()
        self.pages[key].pack(fill="both", expand=True)

    # ── COMPONENT HELPER: CARD AND UI UTILITIES ─────────────────────────
    def _card(self, parent, title=None, accent=True, **kw):
        """Return a single card frame (pack/grid it yourself)."""
        card = tk.Frame(parent, bg=self.card_bg,
                        highlightthickness=1,
                        highlightbackground=self.card_border, **kw)
        if accent:
            tk.Frame(card, bg=self.accent_secondary, height=2).pack(fill="x")
        hdr_frame = None
        if title:
            hdr_frame = tk.Frame(card, bg=self.card_bg)
            hdr_frame.pack(fill="x", padx=12, pady=(8, 0))
            tk.Label(hdr_frame, text=title, bg=self.card_bg,
                     fg=self.text_color, font=("Segoe UI", 8, "bold")).pack(side="left")
        return card, hdr_frame

    def _stat_row(self, parent, label, color=None):
        row = tk.Frame(parent, bg=self.card_bg)
        row.pack(fill="x", padx=14, pady=2)
        tk.Label(row, text=label, bg=self.card_bg, fg=self.text_color,
                 font=("Segoe UI", 9)).pack(side="left")
        val = tk.Label(row, text="—", bg=self.card_bg,
                       fg=color or self.text_color, font=("Segoe UI", 10, "bold"))
        val.pack(side="right")
        return val

    # ── DASHBOARD PAGE: REAL-TIME SESSION ANALYTICS ─────────────────────
    def _build_dashboard(self):
        page = tk.Frame(self.content_area, bg=self.bg_color)
        self.pages["dashboard"] = page

        # CREATE A FLEXIBLE SCROLLING CONTAINER TO ACCOMMODATE DYNAMIC DASHBOARD CARDS
        self.dash_canvas = tk.Canvas(page, bg=self.bg_color, highlightthickness=0)
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.dash_canvas.yview)
        scroll_frame = tk.Frame(self.dash_canvas, bg=self.bg_color)

        # This will hold the window id so we can resize it
        self.dash_window = self.dash_canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        self.dash_canvas.configure(yscrollcommand=vsb.set)

        def _on_dash_scroll(e):
            self.dash_canvas.configure(scrollregion=self.dash_canvas.bbox("all"))
            # Force inner frame to match canvas width
            self.dash_canvas.itemconfig(self.dash_window, width=self.dash_canvas.winfo_width())

        self.dash_canvas.bind("<Configure>", _on_dash_scroll)
        vsb.pack(side="right", fill="y")
        self.dash_canvas.pack(side="left", fill="both", expand=True)

        # Mouse wheel support
        def _on_mousewheel(e):
            self.dash_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        self.dash_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # -- Contents stacked vertically
        col_l = tk.Frame(scroll_frame, bg=self.bg_color)
        col_c = tk.Frame(scroll_frame, bg=self.bg_color)
        col_r = tk.Frame(scroll_frame, bg=self.bg_color)

        col_l.pack(side="top", fill="x", padx=16, pady=8)
        col_c.pack(side="top", fill="both", expand=True, padx=16, pady=8)
        col_r.pack(side="top", fill="x", padx=16, pady=8)

        # PRIMARY FINANCIAL CARD DISPLAYING YANG DROPS AND HOURLY EARNING RATES
        yang_c, _ = self._card(col_l, "YANG STATISTICS")
        yang_c.pack(fill="x", pady=(0, 6))
        tk.Label(yang_c, text="NET WORTH (Yang + Items)", bg=self.card_bg,
                 fg=self.text_color, font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(6, 0))
        self.total_worth_label = tk.Label(yang_c, text="—", bg=self.card_bg,
                                   fg=self.accent_color, font=("Segoe UI", 22, "bold"))
        self.total_worth_label.pack(anchor="w", padx=14, pady=(0, 4))
        tk.Frame(yang_c, bg=self.card_border, height=1).pack(fill="x", padx=14, pady=3)
        self.total_worth_hr_label   = self._stat_row(yang_c, "Per Hour",   self.accent_secondary)
        self.total_worth_min_label = self._stat_row(yang_c, "Per Minute", self.accent_amber)
        tk.Frame(yang_c, bg=self.card_border, height=1).pack(fill="x", padx=14, pady=3)

        tk.Label(yang_c, text="Dropped Yang", bg=self.card_bg,
                 fg=self.text_color, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(2, 0))
        self.yang_label     = self._stat_row(yang_c, "Total",      "#10b981")
        self.yang_per_hour_label  = self._stat_row(yang_c, "Per Hour",   "#34d399")
        self.yang_per_minute_label = self._stat_row(yang_c, "Per Minute", "#6ee7b7")
        tk.Frame(yang_c, bg=self.card_bg, height=6).pack()

        # TIME-BASED METRICS SHOWING SESSION DURATION AND ANALYSIS WINDOWS
        sess_c, _ = self._card(col_l, "SESSION")
        sess_c.pack(fill="x", pady=(0, 6))
        self.interval_label      = self._stat_row(sess_c, "Interval")
        self.window_length_label = self._stat_row(sess_c, "Window")
        tk.Frame(sess_c, bg=self.card_bg, height=6).pack()

        # DUNGEON BOSS KILL TRACKER SHOWING RUN COUNTS FOR VARIOUS BOSS TYPES
        dung_c, _ = self._card(col_l, "DUNGEON RUNS")
        dung_c.pack(fill="both", expand=True)
        self.dungeon_blocks = tk.Frame(dung_c, bg=self.card_bg)
        self.dungeon_blocks.pack(fill="both", expand=True, padx=8, pady=6)

        # MAIN ITEM DISPLAY TABLE FEATURING FILTRATION AND SEARCH CAPABILITIES
        loot_c, loot_hdr = self._card(col_c, "COLLECTED ITEMS")
        loot_c.pack(fill="both", expand=True)
        self.loot_search_var = tk.StringVar()
        self.loot_search_var.trace_add("write", lambda *a: self.refresh_last_stats())
        sf = tk.Frame(loot_hdr, bg=self.input_bg, padx=6, pady=2,
                      highlightthickness=1, highlightbackground=self.input_border)
        sf.pack(side="right")
        tk.Label(sf, text="🔍", bg=self.input_bg, fg=self.text_color,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(sf, textvariable=self.loot_search_var, bg=self.input_bg,
                 fg=self.text_color, insertbackground=self.text_color,
                 relief="flat", font=("Segoe UI", 10), width=22, bd=0).pack(side="left", padx=4)

        tf = tk.Frame(loot_c, bg=self.card_bg)
        tf.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree = ttk.Treeview(tf, columns=("star", "item", "qty", "hr", "val"), 
                                 show="headings", height=12)
        for cid, txt, w, anc in [("star","★",32,"center"),("item","Item Name",180,"w"),
                                  ("qty","Quantity",85,"center"),("hr","Qty/h",80,"center"),
                                  ("val","Value",95,"center")]:
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor=anc, minwidth=w)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tf.grid_rowconfigure(0, weight=1)
        tf.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self.on_tree_click)

    # ── SETTINGS PAGE: MONITORING AND FILE OPTIONS ──────────────────────
    def _build_settings(self):
        page = tk.Frame(self.content_area, bg=self.bg_color)
        self.pages["settings"] = page
        wrap = tk.Frame(page, bg=self.bg_color)
        wrap.pack(fill="both", expand=True, padx=28, pady=22)
        tk.Label(wrap, text="⚙️  Settings", bg=self.bg_color, fg=self.text_color,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 14))

        # CONFIGURATION PANEL FOR SELECTING THE ACTIVE SOURCE LOG FILE
        lc, _ = self._card(wrap, "LOG FILE")
        lc.pack(fill="x", pady=(0, 10))
        lf = tk.Frame(lc, bg=self.card_bg)
        lf.pack(fill="x", padx=14, pady=10)
        tk.Label(lf, text="Log File Path", bg=self.card_bg, fg=self.text_color,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        row = tk.Frame(lf, bg=self.card_bg)
        row.pack(fill="x")
        self.log_path_var = tk.StringVar(value="info_chat_loot.log")
        tk.Entry(row, textvariable=self.log_path_var, bg=self.input_bg, fg=self.text_color,
                 insertbackground=self.text_color, font=("Segoe UI", 10),
                 relief="flat", bd=0, highlightthickness=1, 
                 highlightbackground=self.input_border).pack(side="left", fill="x", expand=True,
                                           padx=(0, 8), ipady=6, ipadx=4)
        ttk.Button(row, text="Browse…", command=self.browse_file,
                   style="Secondary.TButton").pack(side="right")

        # ADJUSTABLE PARAMETERS FOR DATA REFRESH RATES AND SLIDING WINDOW SIZES
        mc, _ = self._card(wrap, "MONITOR SETTINGS")
        mc.pack(fill="x", pady=(0, 10))
        mg = tk.Frame(mc, bg=self.card_bg)
        mg.pack(fill="x", padx=14, pady=10)

        self.window_minutes_var = tk.IntVar(value=120)
        self.refresh_secs_var   = tk.IntVar(value=3)
        self.from_start_var     = tk.BooleanVar(value=False)

        for r, (lbl, var, frm, to, suf) in enumerate([
            ("Window Size",  self.window_minutes_var, 1, 600, "minutes"),
            ("Refresh Rate", self.refresh_secs_var,   1,  60, "seconds"),
        ]):
            tk.Label(mg, text=lbl, bg=self.card_bg, fg=self.text_color,
                     font=("Segoe UI", 10)).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 14))
            tk.Spinbox(mg, from_=frm, to=to, textvariable=var,
                       bg=self.input_bg, fg=self.text_color, relief="flat",
                       insertbackground=self.text_color, font=("Segoe UI", 10),
                       width=8, buttonbackground=self.input_bg,
                       highlightthickness=1, highlightbackground=self.input_border).grid(row=r, column=1, sticky="w", pady=6)
            tk.Label(mg, text=suf, bg=self.card_bg, fg=self.text_color,
                     font=("Segoe UI", 9)).grid(row=r, column=2, sticky="w", padx=6)

        cb = tk.Frame(mc, bg=self.card_bg)
        cb.pack(fill="x", padx=14, pady=(0, 12))
        tk.Checkbutton(cb, text="Read log from the beginning on start",
                       variable=self.from_start_var,
                       bg=self.card_bg, fg=self.text_color,
                       selectcolor=self.input_bg, activebackground=self.card_bg,
                       activeforeground=self.text_color,
                       font=("Segoe UI", 10)).pack(anchor="w")

    # ── MARKET PAGE: ITEM PRICE DATABASE ACCESS ─────────────────────────
    def _build_market(self):
        page = tk.Frame(self.content_area, bg=self.bg_color)
        self.pages["market"] = page
        wrap = tk.Frame(page, bg=self.bg_color)
        wrap.pack(fill="both", expand=True, padx=28, pady=22)

        hrow = tk.Frame(wrap, bg=self.bg_color)
        hrow.pack(fill="x", pady=(0, 12))
        tk.Label(hrow, text="🏷️ Market Prices", bg=self.bg_color, fg=self.text_color,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        sf2 = tk.Frame(hrow, bg=self.input_bg, padx=8, pady=4,
                       highlightthickness=1, highlightbackground=self.input_border)
        sf2.pack(side="right")
        tk.Label(sf2, text="🔍", bg=self.input_bg, fg=self.text_color).pack(side="left")
        self.price_search_var = tk.StringVar()
        self.price_search_var.trace_add("write",
            lambda *a: self.render_price_list(self.price_search_var.get()))
        tk.Entry(sf2, textvariable=self.price_search_var, bg=self.input_bg,
                 fg=self.text_color, insertbackground=self.text_color,
                 relief="flat", font=("Segoe UI", 10), width=24, bd=0).pack(side="left", padx=4)

        cont = tk.Frame(wrap, bg=self.card_border, padx=1, pady=1)
        cont.pack(fill="both", expand=True)
        inner = tk.Frame(cont, bg=self.card_bg)
        inner.pack(fill="both", expand=True)
        self.price_canvas = tk.Canvas(inner, bg=self.card_bg, highlightthickness=0)
        sc = ttk.Scrollbar(inner, orient="vertical", command=self.price_canvas.yview)
        self.scrollable_frame = tk.Frame(self.price_canvas, bg=self.card_bg)
        self.scrollable_frame.bind("<Configure>",
            lambda e: self.price_canvas.configure(
                scrollregion=self.price_canvas.bbox("all")))
        self.price_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.price_canvas.configure(yscrollcommand=sc.set)

        def _mwheel(e):
            self.price_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        self.price_canvas.bind("<Enter>",
            lambda e: self.price_canvas.bind_all("<MouseWheel>", _mwheel))
        self.price_canvas.bind("<Leave>",
            lambda e: self.price_canvas.unbind_all("<MouseWheel>"))

        self.price_canvas.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        self.row_widgets = []
        self.render_price_list()

    # ── UI BLOCKS: DYNAMIC DUNGEON RUN VISUALIZERS ──────────────────────
    def create_dungeon_block(self, parent, name, count, bg):
        f = tk.Frame(parent, bg=bg, padx=14, pady=8,
                     highlightthickness=1, highlightbackground="#374151")
        tk.Label(f, text=name.upper(), bg=bg, fg=self.text_color,
                 font=("Segoe UI", 7, "bold")).pack()
        tk.Label(f, text=str(count), bg=bg, fg="white",
                 font=("Segoe UI", 14, "bold")).pack()
        return f

    def render_dungeon_blocks(self, stats):
        for w in self.dungeon_blocks.winfo_children():
            w.destroy()
        dungeon_data = stats.get("dungeon_runs", {})
        total_runs   = stats.get("total_dungeon_runs", 0)
        if total_runs == 0:
            tk.Label(self.dungeon_blocks, text="No dungeon runs yet.",
                     bg=self.card_bg, fg=self.text_color,
                     font=("Segoe UI", 9)).pack(padx=6, pady=4)
        else:
            blocks = [("Total", total_runs, "#374151")]
            for name in DUNGEONS:
                if name in dungeon_data:
                    blocks.append((name, dungeon_data[name], DUNGEONS[name]["color"]))
            
            for name, count, color in blocks:
                blk = self.create_dungeon_block(self.dungeon_blocks, name, count, color)
                blk.pack(side="left", padx=4, pady=4)

    # ── PRICE LISTING: DYNAMIC MARKET TABLE RENDERER ────────────────────
    def render_price_list(self, filter_text=""):
        if not hasattr(self, "scrollable_frame") or not hasattr(self, "row_widgets"):
            self.row_widgets = []
            return
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.row_widgets.clear()

        hdr = tk.Frame(self.scrollable_frame, bg=self.input_bg)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Item Name", bg=self.input_bg, fg=self.accent_secondary,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=12, pady=6)
        tk.Label(hdr, text="Price (Yang)", bg=self.input_bg, fg=self.accent_secondary,
                 font=("Segoe UI", 9, "bold")).pack(side="right", padx=12)

        for i, (name, price) in enumerate(self.price_db.items()):
            if filter_text.lower() not in name.lower():
                continue
            rb = self.card_bg if i % 2 == 0 else "#1d2030"
            f = tk.Frame(self.scrollable_frame, bg=rb)
            f.pack(fill="x")
            tk.Label(f, text=name, bg=rb, fg=self.text_color,
                     anchor="w", font=("Segoe UI", 10)).pack(side="left", padx=12, pady=5)
            var = tk.StringVar(value=str(price))
            var.trace_add("write", lambda *a, n=name, v=var: self.on_price_change(n, v))
            tk.Entry(f, textvariable=var, bg=self.input_bg, fg=self.accent_color,
                     width=14, justify="right", relief="flat",
                     font=("Segoe UI", 10, "bold"), bd=0,
                     insertbackground=self.text_color,
                     highlightthickness=1, highlightbackground=self.input_border).pack(side="right", padx=10, pady=4, ipady=3)
            self.row_widgets.append((name, var))

        self.scrollable_frame.update_idletasks()
        self.price_canvas.configure(scrollregion=self.price_canvas.bbox("all"))
        if filter_text:
            self.price_canvas.yview_moveto(0)

    # ── MINI MODE: COMPACT OVERLAY WINDOW LOGIC ─────────────────────────
    def toggle_mini_window(self):
        if hasattr(self, "mini_win") and self.mini_win \
                and self.mini_win.winfo_exists():
            self.mini_win.destroy()
            self.mini_win = None
            self.deiconify()
            self.attributes("-alpha", 1.0)
            return
        self.attributes("-alpha", 0.0)
        self.withdraw()
        mw = tk.Toplevel(self)
        self.mini_win = mw
        mw.attributes("-topmost", True)
        mw.overrideredirect(True)
        mw.geometry("240x190")
        if self.mini_pos:
            mw.geometry(f"240x190{self.mini_pos}")
            
        mw.configure(bg="#111318")
        tk.Frame(mw, bg=self.accent_color, height=3).pack(fill="x")

        def _start(e): mw.x, mw.y = e.x, e.y
        def _move(e):
            new_x = mw.winfo_x() + e.x - mw.x
            new_y = mw.winfo_y() + e.y - mw.y
            self.mini_pos = f"+{new_x}+{new_y}"
            mw.geometry(f"240x190{self.mini_pos}")
            self.save_data() # Persist position

        mw.bind("<Button-1>", _start)
        mw.bind("<B1-Motion>", _move)

        tk.Label(mw, text="⚔ JINTEIA TRACKER", fg=self.text_color,
                 bg="#111318", font=("Segoe UI", 7, "bold")).pack(pady=(4, 0))
        self.mini_yang = tk.Label(mw, text="Total: —", fg=self.accent_color,
                                  bg="#111318", font=("Segoe UI", 13, "bold"))
        self.mini_yang.pack(pady=(5, 0))
        self.mini_hr   = tk.Label(mw, text="/hr: —", fg=self.accent_secondary,
                                  bg="#111318", font=("Segoe UI", 10))
        self.mini_hr.pack()
        self.mini_min  = tk.Label(mw, text="/min: —", fg=self.accent_amber,
                                  bg="#111318", font=("Segoe UI", 10))
        self.mini_min.pack()

        self.mini_stop_btn = tk.Label(mw, text="⏹ STOP MONITOR", fg="#ff4444",
                                      bg="#111318", font=("Segoe UI", 8, "bold"), 
                                      cursor="hand2", padx=10, pady=5)
        self.mini_stop_btn.pack(pady=(5, 0))
        self.mini_stop_btn.bind("<Button-1>", lambda e: self.stop_monitor())

        # If not running, show it as disabled
        if self.worker is None:
            self.mini_stop_btn.configure(text="▶ START MONITOR", fg=self.accent_color)
            self.mini_stop_btn.bind("<Button-1>", lambda e: self.start_monitor())

        back = tk.Label(mw, text="[ BACK TO DASHBOARD ]", fg=self.accent_color,
                        bg="#111318", font=("Segoe UI", 7, "bold"), cursor="hand2")
        back.pack(pady=10)
        back.bind("<Button-1>", lambda e: self.toggle_mini_window())

    # ── CORE HELPERS: UI SYNC AND DATA FORMATTING ───────────────────────
    def reset_stats_ui(self):
        for lbl, txt, col in [
            (self.yang_label,            "—", self.accent_color),
            (self.yang_per_hour_label,   "—", self.accent_secondary),
            (self.yang_per_minute_label, "—", self.accent_amber),
            (self.total_worth_label,     "—", "#10b981"),
            (self.total_worth_hr_label,  "—", "#34d399"),
            (self.total_worth_min_label, "—", "#6ee7b7"),
            (self.interval_label,        "Not started", self.text_color),
            (self.window_length_label,   "—", self.text_color),
        ]:
            lbl.configure(text=txt, fg=col)
        self.tree.delete(*self.tree.get_children())
        for w in self.dungeon_blocks.winfo_children():
            w.destroy()

    def update_status(self, text):
        self.status_var.set(text)

    def load_prices(self):
        self.price_db = {}
        path = "prices.json"
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if isinstance(d, dict):
                        self.price_db = d
            except (json.JSONDecodeError, OSError):
                self.price_db = {}
        else:
            self.price_db = {"Shard": 1000}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.price_db, f, indent=4)

    def on_price_change(self, name, var):
        try:
            self.price_db[name] = int(var.get())
            if self.worker:
                self.worker.price_db = self.price_db
            self.save_data()
        except ValueError:
            pass

    def save_data(self):
        with open("prices.json", "w") as f:
            json.dump(self.price_db, f)
        with open("bookmarks.json", "w") as f:
            json.dump(list(self.bookmarks), f)

    def load_bookmarks(self):
        self.bookmarks = set()
        path = "bookmarks.json"
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.bookmarks = set(json.load(f))
            except Exception as e:
                print(f"Error loading bookmarks: {e}")

    def refresh_treeview_filtered(self):
        if not self.last_received_stats:
            return
        sq = self.loot_search_var.get().lower().strip()
        self.tree.delete(*self.tree.get_children())
        items   = self.last_received_stats.get("items", [])
        pinned  = [i for i in items if i[0] in self.bookmarks]
        others  = [i for i in items if i[0] not in self.bookmarks]

        def _ins(batch, is_pin):
            for name, qty, ph, val in batch:
                if sq and sq not in name.lower():
                    continue
                tag  = "pinned" if is_pin else "normal"
                icon = "⭐" if is_pin else "☆"
                self.tree.insert("", "end",
                    values=(icon, name, f"{qty:,}", f"{ph:,}", f"{val:,}"),
                    tags=(tag,))

        _ins(pinned, True)
        _ins(others, False)
        self.tree.tag_configure("pinned", background="#2a2a14", foreground="#fbbf24")
        self.tree.tag_configure("normal", foreground=self.text_color)

    def refresh_last_stats(self):
        self.refresh_treeview_filtered()

    def on_tree_click(self, event):
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid or col != "#1":
            return
        name = self.tree.item(iid, "values")[1]
        if name in self.bookmarks:
            self.bookmarks.remove(name)
        else:
            self.bookmarks.add(name)
        self.save_data()
        self.refresh_last_stats()

    def format_yang_short(self, amount):
        if amount <= 0:
            return "0"
        suffixes = ["", "k", "kk", "kkk", "kkkk"]
        i = min(int(math.floor(math.log10(amount) / 3)), len(suffixes) - 1)
        if i == 0:
            return str(amount)
        return "{:.1f}".format(amount / (1000 ** i)).rstrip("0").rstrip(".") + suffixes[i]

    # ── MONITORING: START AND STOP CONTROL LOGIC ────────────────────────
    def browse_file(self):
        fn = filedialog.askopenfilename(
            title="Select log file",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")])
        if fn:
            self.log_path_var.set(fn)

    def start_monitor(self):
        if self.worker is not None:
            messagebox.showinfo("Info", "Monitor is already running.")
            return
        path = self.log_path_var.get().strip()
        if not path:
            messagebox.showerror("Error", "Please select a log file.")
            return
        self.reset_stats_ui()
        self.stop_event = threading.Event()
        self.worker = LiveMonitorWorker(
            path=path,
            window_minutes=self.window_minutes_var.get(),
            refresh_secs=self.refresh_secs_var.get(),
            from_start=self.from_start_var.get(),
            update_callback=self.schedule_update_stats,
            stop_event=self.stop_event,
            new_event_callback=self._on_new_event
        )
        self.worker.price_db = self.price_db
        self.worker.start()
        self.start_button.configure(state="disabled", bg="#1a3a1a")
        self.stop_button.configure(state="normal")
        self.live_dot.configure(fg=self.accent_color)
        self._animate_status_glow()
        if hasattr(self, "mini_stop_btn") and self.mini_stop_btn.winfo_exists():
            self.mini_stop_btn.configure(text="⏹ STOP MONITOR", fg="#ff4444")
            self.mini_stop_btn.bind("<Button-1>", lambda e: self.stop_monitor())

    def stop_monitor(self):
        if self.worker is not None:
            self.stop_event.set()
            self.worker.join(timeout=1.0)
            self.worker = None
        self.start_button.configure(state="normal", bg=self.accent_color)
        self.stop_button.configure(state="disabled")
        self.live_dot.configure(fg=self.text_color)
        # Stopped state for glow
        self.glow_val = 255 
        if hasattr(self, "mini_stop_btn") and self.mini_stop_btn.winfo_exists():
            self.mini_stop_btn.configure(text="▶ START MONITOR", fg=self.accent_color)
            self.mini_stop_btn.bind("<Button-1>", lambda e: self.start_monitor())

    def on_close(self):
        self.stop_monitor()
        self.destroy()

    # ── STATS PROCESSING: REAL-TIME UI DATA UPDATING ────────────────────
    def schedule_update_stats(self, stats: Dict):
        self.after(0, self.update_stats, stats)

    def update_stats(self, stats: Dict):
        self.last_received_stats = stats
        if "status" in stats:
            self.update_status(stats["status"])
            if len(stats) == 1:
                return
        if "error" in stats:
            messagebox.showerror("Error", stats["error"])
            self.update_status("Error occurred.")
            self.stop_monitor()
            return

        start   = stats["start"]
        end     = stats["end"]
        hours   = stats["hours"]
        minutes = stats["minutes"]
        dy      = stats["dropped_yang"]
        dy_hr   = stats["yang_per_hour"]
        dy_min  = stats["yang_per_minute"]
        items   = stats["items"]

        # FORMAT AND DISPLAY THE START AND END TIMES OF THE CURRENT ANALYSIS WINDOW
        if start.date() != end.date():
            ts = f"{start.strftime('%d/%m %H:%M:%S')} → {end.strftime('%d/%m %H:%M:%S')}"
        else:
            ts = f"{start.strftime('%H:%M:%S')} → {end.strftime('%H:%M:%S')}"
        self.interval_label.configure(text=ts, fg=self.text_color)

        if hours >= 24:
            d = int(hours // 24)
            wt = f"{d} Days {hours%24:.1f} Hours ({minutes:.0f} Minutes)"
        else:
            wt = f"{hours:.2f} Hours ({minutes:.0f} Minutes)"
        self.window_length_label.configure(text=wt, fg=self.text_color)

        self.yang_label.configure(
            text=f"{dy:,} ({self.format_yang_short(dy)})")
        self.yang_per_hour_label.configure(
            text=f"{dy_hr:,}  ({self.format_yang_short(dy_hr)})")
        self.yang_per_minute_label.configure(
            text=f"{dy_min:,}  ({self.format_yang_short(dy_min)})")

        mat  = sum(i[3] for i in items)
        nw   = dy + mat
        nwhr = int(round(nw / hours))   if hours   > 0 else 0
        nwmn = int(round(nw / minutes)) if minutes > 0 else 0
        self.total_worth_label.configure(
            text=f"{nw:,}  ({self.format_yang_short(nw)})")
        self.total_worth_hr_label.configure(
            text=f"{nwhr:,}  ({self.format_yang_short(nwhr)})")
        self.total_worth_min_label.configure(
            text=f"{nwmn:,}  ({self.format_yang_short(nwmn)})")

        # UPDATE MINI-PLAYER OVERLAY WITH REAL-TIME STATISTICAL SNAPSHOTS
        if hasattr(self, "mini_win") and self.mini_win \
                and self.mini_win.winfo_exists():
            try:
                self.mini_yang.configure(text=f"Total: {self.format_yang_short(nw)} Yang")
                self.mini_hr.configure(  text=f"/hr:   {self.format_yang_short(nwhr)}")
                self.mini_min.configure( text=f"/min:  {self.format_yang_short(nwmn)}")
            except Exception:
                pass

        self.render_dungeon_blocks(stats)
        self.refresh_treeview_filtered()
        self._refresh_sound_item_list()

        # AUTOMATICALLY REGISTER NEWLY DISCOVERED ITEMS INTO THE MARKET PRICE DATABASE
        changed = False
        for item in items:
            if item[0] not in self.price_db:
                self.price_db[item[0]] = 0
                changed = True
        if changed:
            self.render_price_list()
            self.save_data()
            self._refresh_sound_item_list()

        pc = stats.get("processed_count", 0)
        li = stats.get("last_item", "—")
        self.update_status(
            f"Read: {pc} lines (Last Drop: {li})  ·  "
            f"Updated {end.strftime('%H:%M:%S')}  ·  "
            f"{len(items)} different items  ·  "
            f"Yang: {self.format_yang_short(nw)}")

    # ── AUDIO LOGIC: SOUND TRIGGERING AND PLAYBACK ──────────────────────
    def _on_new_event(self, ev: LootEvent):
        if not ev.is_yang:
            self.trigger_drop_sound(ev.item)

    def trigger_drop_sound(self, item_name):
        if not self.drop_sounds_enabled.get():
            return
        for rule in self.drop_sounds:
            if rule["item"] == item_name:
                path = rule.get("sound")
                dur = rule.get("duration", self.default_sound_duration.get())
                if path:
                    threading.Thread(target=self._play_rule_sounds,
                                     args=(path, dur), daemon=True).start()

    def _get_sound(self, path):
        if not hasattr(self, "sound_cache"): self.sound_cache = {}
        if path not in self.sound_cache:
            try:
                self.sound_cache[path] = pygame.mixer.Sound(path)
            except: return None
        return self.sound_cache.get(path)

    def _play_rule_sounds(self, path, duration):
        if not path or not os.path.exists(path): return
        try:
            snd = self._get_sound(path)
            if snd:
                ch = snd.play()
                if ch:
                    if duration > 0:
                        self.after(int(duration * 1000), ch.stop)
        except: pass

    # ── DROP SOUNDS: ADVANCED AUDIO RULES INTERFACE ─────────────────────
    def _build_sounds(self):
        page = tk.Frame(self.content_area, bg=self.bg_color)
        self.pages["sounds"] = page
        
        wrap = tk.Frame(page, bg=self.bg_color)
        wrap.pack(fill="both", expand=True, padx=28, pady=22)
        
        row_h = tk.Frame(wrap, bg=self.bg_color)
        row_h.pack(fill="x", pady=(0, 14))
        tk.Label(row_h, text="🔊  Drop Sounds", bg=self.bg_color, fg=self.text_color,
                 font=("Segoe UI", 16, "bold")).pack(side="left")

        # Global Settings Card
        gc, _ = self._card(wrap, "GLOBAL SETTINGS")
        gc.pack(fill="x", pady=(0, 10))
        gf = tk.Frame(gc, bg=self.card_bg)
        gf.pack(fill="x", padx=14, pady=10)
        
        tk.Checkbutton(gf, text="Enable Drop Sounds", variable=self.drop_sounds_enabled,
                       bg=self.card_bg, fg=self.accent_color, selectcolor=self.bg_color,
                       activebackground=self.card_bg, activeforeground=self.accent_color,
                       font=("Segoe UI", 10, "bold"), command=self.save_sounds).pack(side="left")
        
        tk.Label(gf, text="Default Stop (s):", bg=self.card_bg, fg=self.text_color).pack(side="left", padx=(30, 6))
        tk.Spinbox(gf, from_=0.0, to=300.0, increment=0.5, textvariable=self.default_sound_duration,
                   width=5, bg=self.input_bg, fg=self.text_color, relief="flat", buttonbackground=self.input_bg,
                   highlightthickness=1, highlightbackground=self.input_border,
                   command=self.save_sounds).pack(side="left")

        # Add Rule Card
        ac, _ = self._card(wrap, "ADD NEW RULE")
        ac.pack(fill="x", pady=(0, 10))
        af = tk.Frame(ac, bg=self.card_bg)
        af.pack(fill="x", padx=14, pady=10)
        
        # Row 1: Item & Priority
        r1 = tk.Frame(af, bg=self.card_bg)
        r1.pack(fill="x", pady=4)
        tk.Label(r1, text="Item Name / Pattern", bg=self.card_bg, fg=self.text_color, font=("Segoe UI", 9)).pack(side="left")
        self.new_rule_item_var = tk.StringVar()
        self.item_combo = ttk.Combobox(r1, textvariable=self.new_rule_item_var, width=35)
        self.item_combo.pack(side="left", padx=10)
        self._refresh_sound_item_list()
        
        tk.Label(r1, text="Priority:", bg=self.card_bg, fg=self.text_color).pack(side="left", padx=(10, 0))
        self.new_rule_prio_var = tk.IntVar(value=1)
        ttk.Combobox(r1, textvariable=self.new_rule_prio_var, values=[1,2,3,4,5], width=3).pack(side="left", padx=5)

        # Row 2: File
        r2 = tk.Frame(af, bg=self.card_bg)
        r2.pack(fill="x", pady=8)
        tk.Label(r2, text="Sound File:", bg=self.card_bg, fg=self.text_color, font=("Segoe UI", 9)).pack(side="left")
        self.new_rule_sound_var = tk.StringVar()
        tk.Entry(r2, textvariable=self.new_rule_sound_var, bg=self.input_bg, fg=self.text_color,
                 relief="flat", bd=0, font=("Segoe UI", 9),
                 highlightthickness=1, highlightbackground=self.input_border).pack(side="left", fill="x", expand=True, padx=10)
        
        ttk.Button(r2, text="Browse", command=self.add_drop_sound_files, style="Secondary.TButton").pack(side="right")

        # Row 3: Action
        r3 = tk.Frame(af, bg=self.card_bg)
        r3.pack(fill="x", pady=4)
        tk.Label(r3, text="Specific Stop (s):", bg=self.card_bg, fg=self.text_color, font=("Segoe UI", 8)).pack(side="left")
        self.new_rule_dur_var = tk.DoubleVar(value=0.0)
        tk.Spinbox(r3, from_=0.0, to=300.0, increment=0.5, textvariable=self.new_rule_dur_var,
                   width=5, bg=self.input_bg, fg=self.text_color, relief="flat", buttonbackground=self.input_bg,
                   highlightthickness=1, highlightbackground=self.input_border).pack(side="left", padx=5)
        tk.Label(r3, text="(0 = use default - (set sound length to stop long sounds))", bg=self.card_bg, fg=self.muted_text, font=("Segoe UI", 8)).pack(side="left")
        
        ttk.Button(r3, text="➕ Add Rule", command=self.save_new_rule, style="Accent.TButton").pack(side="right")

        # Table Card
        tc, _ = self._card(wrap, "ACTIVE RULES", accent=False)
        tc.pack(fill="both", expand=True)
        self.sounds_tree = ttk.Treeview(tc, columns=("item", "prio", "files", "dur"), show="headings", height=8)
        for cid, txt, w in [("item","Pattern",180), ("prio","Priority",50), ("files","Sound Files",300), ("dur","Stop (s)",80)]:
            self.sounds_tree.heading(cid, text=txt)
            self.sounds_tree.column(cid, width=w, anchor="center" if cid!="item" else "w")
        self.sounds_tree.pack(fill="both", expand=True)
        
        # Action row
        low = tk.Frame(tc, bg=self.card_bg, padx=10, pady=8)
        low.pack(fill="x")
        ttk.Button(low, text="🔊 Test Selected", command=self.test_rule_sound, style="Secondary.TButton").pack(side="left")
        ttk.Button(low, text="🗑️ Delete Selected", command=self.delete_rule, style="Danger.TButton").pack(side="right")
        
        self.refresh_drop_sounds_table()

    def _refresh_sound_item_list(self):
        if not hasattr(self, "item_combo"): return
        
        # 1. Start with items from the session stats (if available)
        session_items = []
        pinned_session = []
        normal_session = []
        
        if self.last_received_stats and "items" in self.last_received_stats:
            for item in self.last_received_stats["items"]:
                name = item[0]
                if name in getattr(self, "bookmarks", set()):
                    pinned_session.append(name)
                else:
                    normal_session.append(name)
        
        # 2. Get all other items from price_db and DUNGEONS
        all_db = set(self.price_db.keys())
        for d_info in DUNGEONS.values():
            for d_item in d_info.get("items", []):
                all_db.add(d_item)

        # Remove those already in session list
        seen = set(pinned_session) | set(normal_session)
        others = sorted(list(all_db - seen))
        
        # 3. Combine: Pinned Session -> Normal Session -> Rest of DB (Alpha)
        self.item_combo['values'] = pinned_session + normal_session + others

    def add_drop_sound_files(self):
        f = filedialog.askopenfilename(title="Select Sound File", filetypes=[("Audio Files", "*.mp3 *.wav")])
        if f: self.new_rule_sound_var.set(f)

    def save_new_rule(self):
        item = self.new_rule_item_var.get().strip()
        if not item: return
        sound = self.new_rule_sound_var.get().strip()
        if not sound: return
        self.drop_sounds.append({
            "item": item, "priority": self.new_rule_prio_var.get(),
            "sound": sound, "duration": self.new_rule_dur_var.get()
        })
        self.save_sounds()
        self.refresh_drop_sounds_table()
        self.new_rule_item_var.set("")
        self.new_rule_sound_var.set("")
        self.new_rule_dur_var.set(0.0)

    def refresh_drop_sounds_table(self):
        self.sounds_tree.delete(*self.sounds_tree.get_children())
        for i, rule in enumerate(self.drop_sounds):
            f_str = os.path.basename(rule.get('sound', 'No file'))
            d_str = f"{rule['duration']}s" if rule['duration'] > 0 else "Default"
            self.sounds_tree.insert("", "end", iid=i, values=(rule['item'], rule['priority'], f_str, d_str))

    def delete_rule(self):
        sel = self.sounds_tree.selection()
        if not sel: return
        idx = int(sel[0])
        del self.drop_sounds[idx]
        self.save_sounds()
        self.refresh_drop_sounds_table()

    def test_rule_sound(self):
        sel = self.sounds_tree.selection()
        if not sel: return
        rule = self.drop_sounds[int(sel[0])]
        threading.Thread(target=self._play_rule_sounds,
                         args=(rule.get('sound'), rule.get('duration', self.default_sound_duration.get())),
                         daemon=True).start()

    # ── PERSISTENCE: LOAD AND SAVE LOCAL CONFIGURATIONS ─────────────────
    def load_data(self):
        # RETRIEVE PREVIOUSLY SAVED MINI-WINDOW COORDINATES FROM THE LOCAL CONFIG FILE
        if os.path.exists("mini_pos.json"):
            try:
                with open("mini_pos.json", "r") as f:
                    self.mini_pos = json.load(f)
            except: pass

    def save_data(self):
        # Bookmarks
        with open("bookmarks.json", "w", encoding="utf-8") as f:
            json.dump(list(self.bookmarks), f)
        # Prices
        with open("prices.json", "w", encoding="utf-8") as f:
            json.dump(self.price_db, f)
        # Sounds
        self.save_sounds()
        # Position
        with open("mini_pos.json", "w") as f:
            json.dump(self.mini_pos, f)

    def load_sounds(self):
        path = "drop_sounds.json"
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.drop_sounds = data.get("rules", [])
                    self.drop_sounds_enabled.set(data.get("enabled", True))
                    self.default_sound_duration.set(data.get("default_dur", 5.0))
            except: pass

    def save_sounds(self):
        data = {
            "rules": self.drop_sounds,
            "enabled": self.drop_sounds_enabled.get(),
            "default_dur": self.default_sound_duration.get()
        }
        with open("drop_sounds.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _animate_status_glow(self):
        if self.worker is None:
            return
        
        self.glow_val += self.glow_dir
        if self.glow_val <= 100:
            self.glow_val = 100
            self.glow_dir = 8
        elif self.glow_val >= 255:
            self.glow_val = 255
            self.glow_dir = -8
            
        color = f"#{self.glow_val:02x}{self.glow_val:02x}{self.glow_val:02x}"
        self.live_dot.configure(fg=self.accent_color if self.glow_val > 180 else self.text_color)
        # DYNAMIC COLOR SHIFTING LOGIC FOR THE STATUS INDICATOR TO CREATE A PULSING EFFECT
        try:
            # We can't easily change individual label colors in a loop without a handle
            # but we can effect the live_dot we have:
            hex_color = "#{:02x}{:02x}{:02x}".format(0, int(self.glow_val * 0.78), int(self.glow_val * 0.32)) # Greenish glow
            self.live_dot.configure(fg=hex_color)
        except: pass

        try:
            self.after(50, self._animate_status_glow)
        except:
            pass

def main():
    app = LootMonitorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
