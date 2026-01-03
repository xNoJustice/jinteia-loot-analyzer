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
    total_yang = 0
    items_qty = defaultdict(int)      # item -> total quantity
    events_list: List[LootEvent] = []

    for ev in events:
        events_list.append(ev)
        if ev.is_yang:
            total_yang += ev.quantity
        else:
            items_qty[ev.item] += ev.quantity

    if not events_list:
        return {
            "total_yang": 0,
            "total_items_qty": 0,
            "items_qty": {},
            "hours": 0.0,
        }

    start = events_list[0].ts
    end = events_list[-1].ts
    elapsed_seconds = max((end - start).total_seconds(), 1)
    hours = elapsed_seconds / 3600.0
    total_items_qty = sum(items_qty.values())

    return {
        "total_yang": total_yang,
        "total_items_qty": total_items_qty,
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

    def add_event(self, ev: LootEvent):
        self.window.append(ev)
        cutoff = ev.ts - dt.timedelta(minutes=self.window_minutes)
        while self.window and self.window[0].ts < cutoff:
            self.window.popleft()

    def compute_stats_from_window(self) -> Optional[Dict]:
        if not self.window:
            return None

        events_list = list(self.window)
        total_yang = sum(ev.quantity for ev in events_list if ev.is_yang)
        items_qty: Dict[str, int] = defaultdict(int)
        for ev in events_list:
            if not ev.is_yang:
                items_qty[ev.item] += ev.quantity

        start = events_list[0].ts
        end = events_list[-1].ts
        elapsed = max((end - start).total_seconds(), 1)
        hours = elapsed / 3600.0
        total_items = sum(items_qty.values())

        # Build per-item stats including per-hour (rounded to int)
        items_list: List[Tuple[str, int, int]] = []
        for name, qty in items_qty.items():
            per_hour = int(round(qty / hours))
            items_list.append((name, qty, per_hour))

        # Sort by quantity desc
        items_list.sort(key=lambda x: x[1], reverse=True)

        stats = {
            "start": start,
            "end": end,
            "hours": hours,
            "total_yang": total_yang,
            "yang_per_hour": int(round(total_yang / hours)),
            "total_items_qty": total_items,
            "items_per_hour": int(round(total_items / hours)),
            "items": items_list,
        }
        return stats

    def run(self):
        try:
            f = open(self.path, "r", encoding="utf-8", errors="ignore")
        except OSError as e:
            # Send error to UI via callback as None with extra key
            self.update_callback({"error": f"Cannot open log file: {e}"})
            return

        if not self.from_start:
            f.seek(0, os.SEEK_END)

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


# ---------------------------------------------------------------------------
# Tkinter UI
# ---------------------------------------------------------------------------

class LootMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Loot Monitor - By Paysami AI slop v1.0 - Download only from there: https://github.com/PaysamiKekW/Jinteia-Loot-Analyzer-FREE/")
        self.geometry("900x600")

        # Slightly more modern feel
        self.configure(bg="#2d2d30")
        self.style = ttk.Style(self)
        try:
            # Use a modern themed style if available
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        # Global style tweaks
        self.style.configure("TFrame", background="#2d2d30")
        self.style.configure("TLabelframe", background="#2d2d30", foreground="#ffffff")
        self.style.configure(
            "TLabelframe.Label",
            background="#2d2d30",
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
        )
        self.style.configure(
            "TLabel",
            background="#2d2d30",
            foreground="#ffffff",
            font=("Segoe UI", 9),
        )
        self.style.configure(
            "TButton",
            font=("Segoe UI", 9),
            padding=4,
        )
        self.style.configure(
            "Treeview",
            font=("Segoe UI", 9),
            rowheight=22,
        )
        self.style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
        )

        self.stop_event = threading.Event()
        self.worker: Optional[LiveMonitorWorker] = None

        self.create_widgets()

    # -------------------- UI layout -------------------- #

    def create_widgets(self):
        # Top frame: settings
        settings_frame = ttk.LabelFrame(self, text="Settings")
        settings_frame.pack(fill="x", padx=10, pady=8)

        # Log file path
        ttk.Label(settings_frame, text="Log file:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.log_path_var = tk.StringVar(value="info_chat_loot.log")
        self.log_path_entry = ttk.Entry(settings_frame, textvariable=self.log_path_var, width=50)
        self.log_path_entry.grid(row=0, column=1, sticky="w", padx=5, pady=5)

        browse_btn = ttk.Button(settings_frame, text="Browse...", command=self.browse_file)
        browse_btn.grid(row=0, column=2, sticky="w", padx=5, pady=5)

        # Window minutes
        ttk.Label(settings_frame, text="Window (minutes):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.window_minutes_var = tk.IntVar(value=60)
        ttk.Entry(settings_frame, textvariable=self.window_minutes_var, width=10).grid(
            row=1, column=1, sticky="w", padx=5, pady=5
        )

        # Refresh seconds
        ttk.Label(settings_frame, text="Refresh every (seconds):").grid(
            row=1, column=2, sticky="w", padx=5, pady=5
        )
        self.refresh_secs_var = tk.IntVar(value=5)
        ttk.Entry(settings_frame, textvariable=self.refresh_secs_var, width=10).grid(
            row=1, column=3, sticky="w", padx=5, pady=5
        )

        # From start checkbox
        self.from_start_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            settings_frame,
            text="Read file from beginning (otherwise only new lines)",
            variable=self.from_start_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=5)

        # Start/Stop buttons
        buttons_frame = ttk.Frame(settings_frame)
        buttons_frame.grid(row=3, column=0, columnspan=4, sticky="w", padx=5, pady=5)

        self.start_button = ttk.Button(buttons_frame, text="Start Live Monitor", command=self.start_monitor)
        self.start_button.pack(side="left", padx=5)

        self.stop_button = ttk.Button(buttons_frame, text="Stop", command=self.stop_monitor, state="disabled")
        self.stop_button.pack(side="left", padx=5)

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=5)

        # Info frame: overall stats
        info_frame = ttk.LabelFrame(self, text="Current Window Statistics")
        info_frame.pack(fill="x", padx=10, pady=5)

        self.interval_label = ttk.Label(info_frame, text="Interval: -")
        self.interval_label.grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=2)

        self.window_length_label = ttk.Label(info_frame, text="Window length: - h")
        self.window_length_label.grid(row=1, column=0, sticky="w", padx=5, pady=2)

        self.yang_label = ttk.Label(info_frame, text="Yang in window: -")
        self.yang_label.grid(row=2, column=0, sticky="w", padx=5, pady=2)

        self.yang_per_hour_label = ttk.Label(info_frame, text="Estimated Yang/hour: -")
        self.yang_per_hour_label.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        self.items_label = ttk.Label(info_frame, text="Items (without Yang): -")
        self.items_label.grid(row=3, column=0, sticky="w", padx=5, pady=2)

        self.items_per_hour_label = ttk.Label(info_frame, text="Estimated items/hour: -")
        self.items_per_hour_label.grid(row=3, column=1, sticky="w", padx=5, pady=2)

        # Items list (with scrollbar)
        items_frame = ttk.LabelFrame(self, text="Items in window (sorted by quantity)")
        items_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("item", "quantity", "per_hour")
        self.tree = ttk.Treeview(items_frame, columns=columns, show="headings")
        self.tree.heading("item", text="Item")
        self.tree.heading("quantity", text="Quantity")
        self.tree.heading("per_hour", text="Quantity/hour")

        self.tree.column("item", width=300, anchor="w")
        self.tree.column("quantity", width=100, anchor="e")
        self.tree.column("per_hour", width=120, anchor="e")

        vsb = ttk.Scrollbar(items_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Striped rows for nicer look
        self.tree.tag_configure("evenrow", background="#f0f0f0")
        self.tree.tag_configure("oddrow", background="#e0e0e0")

    # -------------------- UI helpers -------------------- #

    def reset_stats_ui(self):
        """Clear stats and item list for a fresh start."""
        self.interval_label.config(text="Interval: -")
        self.window_length_label.config(text="Window length: - h")
        self.yang_label.config(text="Yang in window: -")
        self.yang_per_hour_label.config(text="Estimated Yang/hour: -")
        self.items_label.config(text="Items (without Yang): -")
        self.items_per_hour_label.config(text="Estimated items/hour: -")
        self.tree.delete(*self.tree.get_children())

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
        if "error" in stats:
            messagebox.showerror("Error", stats["error"])
            self.stop_monitor()
            return

        start = stats["start"]
        end = stats["end"]
        hours = stats["hours"]
        total_yang = stats["total_yang"]
        yang_per_hour = stats["yang_per_hour"]
        total_items_qty = stats["total_items_qty"]
        items_per_hour = stats["items_per_hour"]
        items_list = stats["items"]

        self.interval_label.config(
            text=f"Interval: {start}  ->  {end}"
        )
        self.window_length_label.config(text=f"Window length: {hours:.2f} h")
        self.yang_label.config(text=f"Yang in window: {total_yang:,}".replace(",", " "))
        self.yang_per_hour_label.config(
            text=f"Estimated Yang/hour: {yang_per_hour:,.0f}".replace(",", " ")
        )
        self.items_label.config(
            text=f"Items (without Yang): {total_items_qty:,}".replace(",", " ")
        )
        self.items_per_hour_label.config(
            text=f"Estimated items/hour: {items_per_hour:,.0f}".replace(",", " ")
        )

        # Update items tree
        self.tree.delete(*self.tree.get_children())
        for idx, (name, qty, per_hour) in enumerate(items_list):
            tag = "evenrow" if idx % 2 == 0 else "oddrow"
            self.tree.insert(
                "",
                "end",
                values=(
                    name,
                    f"{qty:,}".replace(",", " "),
                    f"{per_hour:,.0f}".replace(",", " "),
                ),
                tags=(tag,),
            )


def main():
    app = LootMonitorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()