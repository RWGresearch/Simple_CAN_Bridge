# REVISION: 7
"""
CAN BRIDGE + LOGGER
--------------------
Bridges two independent CAN adapters (CAN 1 <-> CAN 2), forwarding every
frame seen on either one straight to the other, and/or logs traffic on
either/both to a .trc trace file (PCAN-Explorer v2.1 format, readable by
PCAN-View / PCAN-Explorer and any tooling built around that format).

Each channel is configured independently and can be either:
  - a PEAK PCAN-USB adapter (python-can 'pcan' backend), or
  - a CANable-style USB-to-CAN adapter running the SLCAN firmware
    (python-can 'slcan' backend, connects over a virtual COM/serial port).

Convention: CAN 1 and CAN 2 are just the two bridge sides - neither has a
fixed meaning; label them however your setup needs.

Install: pip install python-can  (PEAK PCAN-Basic drivers required for the
PCAN backend; the SLCAN backend only needs pyserial, which python-can pulls
in). Serial port auto-listing (Refresh button) needs pyserial's
serial.tools.list_ports - falls back to manual port entry if unavailable.
See INSTALL.md for full setup steps.

Rev 1: initial version - per-channel adapter type (PCAN / SLCAN) + connect,
       plain bidirectional pass-through bridge (Start/Stop bridge button,
       no per-ID filtering), .trc trace logging (channel/direction
       selectable, Start/Stop logging button, Save-As dialog), one live CAN
       data list per adapter (latest frame per ID, with its own Clear
       button), and an app log box.
Rev 2: Channel._open_bus() now passes 'interface=' instead of the
       deprecated 'bustype=' kwarg to can.interface.Bus() (python-can
       warned it's removed in v5.0.0) - no behaviour change.
Rev 3: DBC sender-node lookup - a "Load DBC..." button (Connections panel)
       opens a file picker (multi-select) that regex-scans each chosen
       .dbc's BO_ lines for "ID -> transmitting node name" (no signal
       decoding, just the sender name - e.g. VCM, LBC), merged across every
       file picked. Both live-data lists gained a Node column showing that
       name per ID (blank if no DBC loaded or the ID isn't in it).
Rev 4: clearer missing-dependency messages - the python-can/pyserial
       startup warnings now include the exact pip install command and say
       what's actually blocked (python-can blocks both adapter types,
       pyserial only blocks SLCAN port auto-detect). The SLCAN Refresh
       button also logs a message instead of silently doing nothing when
       pyserial isn't installed.
Rev 5: Connect no longer freezes the window - opening the adapter (a
       SLCAN COM port that isn't actually present can block for a long
       time at the OS level before failing) now happens on a background
       thread instead of the GUI thread. The button reads "Connecting..."
       (disabled) meanwhile and the row updates to Disconnect/failed once
       the attempt finishes, same as before.
Rev 6: Connect attempts can be cancelled - while a connect is in flight
       the button reads "Cancel" (clickable); clicking it hands the row
       back immediately ("not connected") without waiting for the stuck
       OS-level open to give up. There's no way to truly kill a blocked
       serial-port open from Python, so the background attempt keeps
       running unseen - if it later does succeed, it's auto-closed instead
       of leaving an orphaned connection.
Rev 7: the app log box's contents are now also persisted to Logs/app.log
       (appended across runs, one line per log entry, flushed immediately)
       so history survives closing the app. Logs/ is excluded from git.
"""

import bisect
import datetime
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog

try:
    import can
    CAN_AVAILABLE = True
except ImportError:
    CAN_AVAILABLE = False

try:
    from serial.tools import list_ports
    LIST_PORTS_AVAILABLE = True
except ImportError:
    LIST_PORTS_AVAILABLE = False

APP_REVISION = 3
APP_REV_DATE = "2026-09-08"
APP_AUTHOR = "~Russ Gries"
APP_WEBSITE = "RWGresearch.com"

PCAN_CHANNELS = ['PCAN_USBBUS1', 'PCAN_USBBUS2', 'PCAN_USBBUS3', 'PCAN_USBBUS4']
BITRATES = [125000, 250000, 500000, 1000000]
SLCAN_TTY_BAUD = 115200   # USB-serial baud rate to the SLCAN adapter itself

# dark theme palette
BG, PANEL, FIELD = '#1e1e22', '#2a2a30', '#35353c'
FG, FG_DIM, ACC, ERR, OK = '#dcdcdc', '#9a9aa0', '#4da3ff', '#ff6b6b', '#5fd38d'


def default_log_folder():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logs')


def default_trace_folder():
    return os.path.join(default_log_folder(), 'Captures')


def default_app_log_path():
    return os.path.join(default_log_folder(), 'app.log')


DIR_SETS = {'both': {'RX', 'TX'}, 'rx': {'RX'}, 'tx': {'TX'}}


def default_trace_filename(mode, direction='both'):
    """mode: 1 = CAN1 only, 2 = CAN2 only, 3 = both.
    direction: 'both', 'rx' or 'tx' - appended to the name when filtered."""
    tag = {1: 'CAN1', 2: 'CAN2', 3: 'BOTH'}[mode]
    if direction != 'both':
        tag += '_' + direction.upper()
    return time.strftime('Trace_%Y-%m-%d_%H-%M-%S_') + tag + '.trc'


def list_serial_ports():
    if not LIST_PORTS_AVAILABLE:
        return []
    try:
        return sorted(p.device for p in list_ports.comports())
    except Exception:
        return []


_DBC_BO_RE = re.compile(r'BO_ (\d+) \w+: \d+ (\w+)')


def load_dbc_tx_nodes(paths):
    """ID -> transmitting node name, merged from one or more .dbc files
    (BO_ lines only - a lightweight regex scan, not a real DBC parser, so it
    picks up just the sender name per message, no signal decoding). Later
    files in `paths` win on a shared ID. A DBC's explicit 'no assigned
    sender' (Vector__XXX) maps to 'Unknown'; IDs never seen in any chosen
    file are simply absent from the returned dict.

    DBC BO_ lines store extended (29-bit) CAN IDs with bit 31 set as an
    'is-extended' flag (Vector CANdb++ convention) - that bit is masked off
    here so the stored ID matches the raw 29-bit arbitration ID this app
    actually sees on the wire."""
    nodes = {}
    for path in paths:
        try:
            with open(path, 'r', errors='replace') as f:
                for line in f:
                    m = _DBC_BO_RE.match(line)
                    if m:
                        node = m.group(2)
                        raw_id = int(m.group(1))
                        aid = raw_id & 0x1FFFFFFF if raw_id & 0x80000000 \
                            else raw_id
                        nodes[aid] = 'Unknown' if node == 'Vector__XXX' \
                            else node
        except OSError:
            pass
    return nodes


# ── .trc trace logger (PCAN-Explorer v2.1 file format) ───────────────────────
class TrcLogger:
    """Writes CAN frames to a .trc file in PCAN-Explorer v2.1 format
    (columns N,O,T,B,I,d,R,L,D), readable by PCAN-View / PCAN-Explorer.
    Thread-safe: frames arrive from either channel's RX thread as well as
    bridge-forwarded TX. The bus column keeps the real channel number (1 or
    2) so a 'both' log can be split/filtered later."""

    def __init__(self, path, channels, directions=None):
        self.path = path
        self.channels = channels          # set of channel indexes to record
        self.directions = directions or {'RX', 'TX'}   # {'RX'}, {'TX'} or both
        self.count = 0
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self._last_flush = self._t0
        now = datetime.datetime.now()
        delta = now - datetime.datetime(1899, 12, 30)   # PCAN day-number epoch
        days = delta.days + delta.seconds / 86400 + delta.microseconds / 86400e6
        chan_txt = ' + '.join(f"CAN {c}" for c in sorted(channels))
        if self.directions == {'RX', 'TX'}:
            dir_txt = ''
        else:
            dir_txt = ', RX only' if self.directions == {'RX'} else ', TX only'
        self._f = open(path, 'w')
        self._f.write(
            ";$FILEVERSION=2.1\n"
            f";$STARTTIME={days:.13f}\n"
            ";$COLUMNS=N,O,T,B,I,d,R,L,D\n"
            ";\n"
            f";   Start time: {now.month}/{now.day}/{now.year} "
            f"{now:%H:%M:%S}.{now.microsecond // 1000:03d}.0\n"
            f";   Generated by CAN Bridge Logger "
            f"(logging {chan_txt}{dir_txt})\n"
            ";-------------------------------------------------------------------------------\n"
            ";   Bus  Connection   Net Connection   Protocol  Bit rate\n"
            + ''.join(f";   {c}    CAN{c}         bridge_ch{c}     CAN       "
                      "see connection\n" for c in sorted(channels)) +
            ";-------------------------------------------------------------------------------\n"
            ";   Message    Time    Type    ID     Rx/Tx\n"
            ";   Number     Offset  |  Bus  [hex]  |  Reserved\n"
            ";   |          [ms]    |  |    |      |  |  Data Length Code\n"
            ";   |          |       |  |    |      |  |  |    Data [hex] ...\n"
            ";   |          |       |  |    |      |  |  |    |\n"
            ";---+--- ------+------ +- +- --+----- +- +- +--- +- -- -- -- -- -- -- --\n")

    def log_frame(self, ch, direction, aid, data, extended=False):
        if ch not in self.channels or direction not in self.directions:
            return
        with self._lock:
            if self._f is None:
                return
            now = time.perf_counter()
            self.count += 1
            id_field = f"{aid:08X}" if extended else f"{aid:04X}"
            self._f.write(
                f"{self.count:8d}{(now - self._t0) * 1000:14.3f} DT {ch}"
                f"{id_field:>10} {'Tx' if direction == 'TX' else 'Rx'} -  "
                f"{len(data)}    {data.hex(' ').upper()}\n")
            if now - self._last_flush > 1.0:    # survive a crash/unplug
                self._last_flush = now
                self._f.flush()

    def close(self):
        with self._lock:
            if self._f is not None:
                self._f.close()
                self._f = None


# ── CAN channel: RX thread + PCAN/SLCAN backend selection ────────────────────
class Channel:
    def __init__(self, idx, core):
        self.idx = idx          # 1 or 2 - the two bridge sides
        self.core = core
        self.bus = None
        self.rx_count = 0
        self.tx_count = 0
        self.adapter_type = None
        self.channel_name = None
        self.bitrate = None
        self._stop = threading.Event()
        self._thread = None
        self._bus_lock = threading.Lock()

    @property
    def connected(self):
        return self.bus is not None

    def connect(self, adapter_type, channel_name, bitrate):
        self.adapter_type = adapter_type
        self.channel_name = channel_name
        self.bitrate = bitrate
        self.bus = self._open_bus(adapter_type, channel_name, bitrate)
        self.rx_count = self.tx_count = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._thread.start()
        self.core.log(f"CAN{self.idx} connected: {adapter_type} {channel_name} "
                      f"@ {bitrate} bit/s")

    def _open_bus(self, adapter_type, channel_name, bitrate):
        if adapter_type == 'PCAN':
            return can.interface.Bus(interface='pcan', channel=channel_name,
                                     bitrate=bitrate)
        return can.interface.Bus(interface='slcan', channel=channel_name,
                                 bitrate=bitrate, ttyBaudrate=SLCAN_TTY_BAUD)

    def disconnect(self):
        if not self.connected:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._bus_lock:
            bus, self.bus = self.bus, None
        try:
            bus.shutdown()
        except Exception:
            pass
        self.core.log(f"CAN{self.idx} disconnected")

    def send(self, arb_id, data, extended=False):
        with self._bus_lock:
            bus = self.bus
        if bus is None:
            return
        bus.send(can.Message(arbitration_id=arb_id, data=data,
                             is_extended_id=extended))
        self.tx_count += 1
        self.core.mon_update(self.idx, 'TX', arb_id, data, extended)

    def _rx_loop(self):
        while not self._stop.is_set():
            with self._bus_lock:
                bus = self.bus
            if bus is None:
                break
            try:
                msg = bus.recv(timeout=0.1)
            except Exception as exc:
                self.core.log(f"CAN{self.idx} RX error: {exc}")
                time.sleep(0.5)
                continue
            if msg is None:
                continue
            if getattr(msg, 'is_error_frame', False) or \
                    getattr(msg, 'is_remote_frame', False):
                continue
            self.rx_count += 1
            self.core.mon_update(self.idx, 'RX', msg.arbitration_id,
                                 bytes(msg.data), msg.is_extended_id)
            self.core.on_rx(self.idx, msg)


# ── Core: bridge forwarding + trace logging + live monitor ───────────────────
class Core:
    def __init__(self, log):
        self.log = log
        self.ch = {1: Channel(1, self), 2: Channel(2, self)}
        self.bridge_on = False
        self.trc = None
        # live data monitor, one dict per channel: (dir, id) -> [count,
        # last_t (perf_counter), period_ms, data, extended]
        self.mon = {1: {}, 2: {}}
        self.mon_lock = threading.Lock()

    def mon_update(self, ch_idx, direction, aid, data, extended):
        trc = self.trc                    # local ref: stop_logging can race
        if trc is not None:
            trc.log_frame(ch_idx, direction, aid, data, extended)
        now = time.perf_counter()
        with self.mon_lock:
            table = self.mon[ch_idx]
            key = (direction, aid)
            prev = table.get(key)
            period_ms = (now - prev[1]) * 1000 if prev else None
            count = (prev[0] + 1) if prev else 1
            table[key] = [count, now, period_ms, data, extended]

    def on_rx(self, src_idx, msg):
        if not self.bridge_on:
            return
        dst = self.ch[2] if src_idx == 1 else self.ch[1]
        if not dst.connected:
            return
        try:
            dst.send(msg.arbitration_id, bytes(msg.data),
                     extended=msg.is_extended_id)
        except Exception as exc:
            self.log(f"Bridge forward CAN{src_idx}->CAN{3 - src_idx} "
                     f"failed: {exc}")

    def set_bridge(self, on):
        if on and not (self.ch[1].connected and self.ch[2].connected):
            self.log("Bridge needs BOTH channels connected")
            return False
        self.bridge_on = on
        self.log(f"Bridge {'STARTED' if on else 'stopped'}")
        return True

    # -- trace logging -------------------------------------------------------
    def start_logging(self, mode, direction='both', path=None):
        """mode: 1 = CAN1 only, 2 = CAN2 only, 3 = both.
        direction: 'both', 'rx' (received frames only) or 'tx' (frames this
        app sent - bridge-forwarded frames - only)."""
        channels = {1: {1}, 2: {2}, 3: {1, 2}}[mode]
        tag = {1: 'CAN1', 2: 'CAN2', 3: 'BOTH'}[mode]
        directions = DIR_SETS[direction]
        if path is None:
            path = os.path.join(default_trace_folder(),
                                default_trace_filename(mode, direction))
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.trc = TrcLogger(path, channels, directions)
        except OSError as exc:
            self.log(f"Logging failed: {exc}")
            return None
        dir_txt = '' if direction == 'both' else f', {direction.upper()} only'
        self.log(f"Logging {tag}{dir_txt} -> {path}")
        return path

    def stop_logging(self):
        trc = self.trc
        if trc is not None:
            self.trc = None
            trc.close()
            self.log(f"Logging stopped: {trc.count} frames -> "
                     f"{os.path.basename(trc.path)}")

    def shutdown(self):
        self.stop_logging()
        for ch in self.ch.values():
            ch.disconnect()


# ── tkinter UI ─────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"CAN Bridge + Logger  (rev {APP_REVISION})")
        root.geometry("1180x860")
        root.minsize(980, 640)
        root.configure(bg=BG)
        self._style()
        self.logq = queue.Queue()
        self.core = Core(self.logq.put)
        os.makedirs(default_log_folder(), exist_ok=True)
        self._app_log_f = open(default_app_log_path(), 'a', encoding='utf-8')
        self._app_log_f.write(
            f"\n--- session started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self._app_log_f.flush()
        self.ch_ui = {}     # idx -> dict of widgets/vars
        self.tree = {}      # idx -> Treeview
        self.pending = {}   # idx -> gen, present while a connect is in flight
        self.conn_gen = {1: 0, 2: 0}   # idx -> attempt counter, so a stale/
                                       # cancelled attempt's result can be
                                       # told apart from the current one
        self._mon_sorted = {1: [], 2: []}
        self.dbc_nodes = {}   # CAN ID -> sender node name, from Load DBC...

        main = ttk.Frame(root, padding=6)
        main.pack(fill='both', expand=True)

        # ── connections panel ──
        conn = ttk.LabelFrame(main, text="Connections", padding=6)
        conn.pack(fill='x', pady=(0, 4))
        for i in (1, 2):
            self._build_conn_row(conn, i)
        ttk.Button(conn, text="About", width=10,
                   command=self._show_about).grid(row=0, column=9, rowspan=2,
                                                   padx=(12, 2), sticky='ns')
        conn.grid_columnconfigure(8, weight=1)

        dbc_row = ttk.Frame(conn)
        dbc_row.grid(row=2, column=0, columnspan=9, sticky='w', pady=(4, 0))
        ttk.Button(dbc_row, text="Load DBC...", width=12,
                   command=self._load_dbc).grid(row=0, column=0)
        self.dbc_lbl = ttk.Label(dbc_row, text="no DBC loaded",
                                 foreground=FG_DIM)
        self.dbc_lbl.grid(row=0, column=1, padx=8, sticky='w')
        self._help_btn(dbc_row, 0, 2, "Load DBC...",
            "Pick one or more .dbc files (multi-select) to look up each "
            "CAN ID's sender node name (e.g. VCM, LBC) in both live-data "
            "lists' Node column.\n\n"
            "This is a lightweight scan of the DBC's BO_ lines for the "
            "sender name only - it does not decode signal values. If more "
            "than one file defines the same ID, the last one you picked "
            "wins. IDs not found in any loaded DBC show a blank Node.")

        # ── bridge panel ──
        br = ttk.LabelFrame(main, text="Bridge (pass-through CAN1 <-> CAN2)",
                            padding=6)
        br.pack(fill='x', pady=4)
        self.btn_br = ttk.Button(br, text="Start bridge",
                                 command=self.toggle_bridge)
        self.btn_br.grid(row=0, column=0, padx=4)
        self.lbl_br = ttk.Label(br, text="bridge off", foreground=FG_DIM)
        self.lbl_br.grid(row=0, column=1, padx=8, sticky='w')
        self._help_btn(br, 0, 2,
            "Bridge mode",
            "Forwards every frame received on either connected adapter "
            "straight to the other, unmodified, in both directions - "
            "needs both CAN 1 and CAN 2 connected first.\n\n"
            "No per-ID filtering: every frame crosses. Stop the bridge (or "
            "disconnect a channel) to cut it.")

        # ── trace logging panel ──
        lg = ttk.LabelFrame(main, text="Trace logging (.trc)", padding=6)
        lg.pack(fill='x', pady=4)
        self.logmode = tk.IntVar(value=3)
        for i, (val, txt) in enumerate([(1, "Log CAN 1"), (2, "Log CAN 2"),
                                        (3, "Log both")]):
            ttk.Radiobutton(lg, text=txt, value=val,
                            variable=self.logmode).grid(row=0, column=i,
                                                        padx=5, sticky='w')
        ttk.Label(lg, text="Direction:").grid(row=1, column=0, sticky='e')
        self.logdir = tk.StringVar(value='both')
        for i, (val, txt) in enumerate([('both', "Both (RX + TX)"),
                                        ('rx', "RX only"),
                                        ('tx', "TX only")]):
            ttk.Radiobutton(lg, text=txt, value=val,
                            variable=self.logdir).grid(row=1, column=i + 1,
                                                       padx=5, sticky='w')
        self.btn_log = ttk.Button(lg, text="Start logging",
                                  command=self.toggle_logging)
        self.btn_log.grid(row=0, column=3, padx=10)
        self.log_lbl = ttk.Label(lg, text="not logging", foreground=FG_DIM)
        self.log_lbl.grid(row=0, column=4, padx=6, sticky='w', columnspan=2)
        self._help_btn(lg, 1, 4,
            "Trace logging",
            "Records CAN traffic to a .trc file in PCAN-Explorer v2.1 "
            "format, readable directly by PCAN-View / PCAN-Explorer and "
            "any tooling built around that format.\n\n"
            "Log CAN 1 / CAN 2: only that adapter's traffic. Log both: "
            "everything on both, with the Bus column keeping the channel "
            "number (1/2) so the two sides can be told apart later.\n\n"
            "Direction: RX = frames that actually arrived on that "
            "adapter's wire. TX = frames this app sent there (bridge-"
            "forwarded frames from the other side). Both is everything.\n\n"
            "Start logging opens a Save As dialog defaulting to "
            "Logs\\Captures\\ with a timestamped name. Channel/direction "
            "are fixed while a log runs - stop and restart to change them. "
            "The file is flushed every second.")

        # ── stats bar ──
        bar = ttk.Frame(main, padding=(0, 2))
        bar.pack(fill='x')
        self.stats = ttk.Label(bar, text="")
        self.stats.pack(side='left')

        # ── two live-data lists, side by side ──
        lists = ttk.Frame(main)
        lists.pack(fill='both', expand=True, pady=(2, 0))
        lists.grid_columnconfigure(0, weight=1)
        lists.grid_columnconfigure(1, weight=1)
        lists.grid_rowconfigure(0, weight=1)
        for col, idx in enumerate((1, 2)):
            self._build_mon_panel(lists, idx, col)

        # ── app log ──
        logf = ttk.LabelFrame(main, text="Log", padding=2)
        logf.pack(fill='x', pady=(4, 0))
        self.logbox = scrolledtext.ScrolledText(
            logf, width=32, height=7, state='disabled', wrap='word',
            bg='#141417', fg=OK, insertbackground=FG, relief='flat')
        self.logbox.pack(fill='both', expand=True)

        if not CAN_AVAILABLE:
            self.logq.put("python-can not installed - run: pip install "
                          "python-can  (needed before connecting either "
                          "PCAN or SLCAN adapter)")
        if not LIST_PORTS_AVAILABLE:
            self.logq.put("pyserial not installed - run: pip install "
                          "pyserial  (only needed for SLCAN port "
                          "auto-detect/refresh; type the COM port manually "
                          "until then)")
        self._poll_n = 0
        self.root.after(100, self._poll)

    # ── one connection row ──
    def _build_conn_row(self, conn, idx):
        row = idx - 1
        ttk.Label(conn, text=f"CAN {idx}", width=6).grid(
            row=row, column=0, sticky='w', pady=2)

        type_var = tk.StringVar(value='PCAN')
        type_cb = ttk.Combobox(conn, textvariable=type_var,
                               values=('PCAN', 'SLCAN'), width=7,
                               state='readonly')
        type_cb.grid(row=row, column=1, padx=4)

        chan_var = tk.StringVar(value=PCAN_CHANNELS[idx - 1])
        chan_cb = ttk.Combobox(conn, textvariable=chan_var,
                               values=PCAN_CHANNELS, width=15,
                               state='readonly')
        chan_cb.grid(row=row, column=2, padx=4)

        refresh_btn = ttk.Button(conn, text="↻", width=2,
                                 style='Small.TButton',
                                 command=lambda n=idx: self._refresh_ports(n))
        refresh_btn.grid(row=row, column=3, padx=(0, 4))

        rate_var = tk.StringVar(value='500000')
        rate_cb = ttk.Combobox(conn, textvariable=rate_var,
                               values=[str(b) for b in BITRATES], width=8,
                               state='readonly')
        rate_cb.grid(row=row, column=4, padx=4)
        ttk.Label(conn, text="bit/s").grid(row=row, column=5, sticky='w')

        btn = ttk.Button(conn, text="Connect", width=11,
                         command=lambda n=idx: self.toggle_conn(n))
        btn.grid(row=row, column=6, padx=6)
        lbl = ttk.Label(conn, text="not connected", foreground=ERR, width=30)
        lbl.grid(row=row, column=7, padx=4, sticky='w')

        type_cb.bind('<<ComboboxSelected>>',
                     lambda e, n=idx: self._adapter_type_changed(n))
        self.ch_ui[idx] = {'type': type_var, 'type_cb': type_cb,
                           'chan': chan_var, 'chan_cb': chan_cb,
                           'rate': rate_var, 'btn': btn, 'lbl': lbl}
        if idx == 1:
            self._help_btn(conn, row, 8, "Adapter type",
                "PCAN: a PEAK PCAN-USB adapter (python-can 'pcan' backend) "
                "- pick its channel (PCAN_USBBUS1/2/...).\n\n"
                "SLCAN: a CANable-style USB-to-CAN adapter running the "
                "SLCAN firmware (python-can 'slcan' backend) - the channel "
                "box becomes the serial/COM port it enumerates as; use the "
                "↻ button to rescan ports. Bitrate must match the "
                "other end of whatever bus this adapter is plugged into.")

    def _adapter_type_changed(self, idx):
        ui = self.ch_ui[idx]
        if ui['type'].get() == 'PCAN':
            ui['chan_cb'].config(values=PCAN_CHANNELS, state='readonly')
            ui['chan'].set(PCAN_CHANNELS[idx - 1])
        else:
            self._refresh_ports(idx)
            ui['chan_cb'].config(state='normal')   # editable: port may not
                                                    # be auto-detected yet

    def _refresh_ports(self, idx):
        ui = self.ch_ui[idx]
        if ui['type'].get() != 'SLCAN':
            return
        if not LIST_PORTS_AVAILABLE:
            self.logq.put("can't scan ports - pyserial not installed "
                          "(run: pip install pyserial); type the COM port "
                          "manually")
            return
        ports = list_serial_ports()
        ui['chan_cb'].config(values=ports)
        if ports and ui['chan'].get() not in ports:
            ui['chan'].set(ports[0])

    # ── one live-data panel ──
    def _build_mon_panel(self, parent, idx, col):
        monf = ttk.LabelFrame(parent, text=f"CAN {idx} live data "
                              "(latest frame per ID)", padding=2)
        monf.grid(row=0, column=col, sticky='nsew',
                 padx=(0, 4) if col == 0 else (4, 0))
        top = ttk.Frame(monf)
        top.pack(side='top', fill='x')
        ttk.Button(top, text="Clear", width=8, style='Small.TButton',
                   command=lambda n=idx: self._clear_monitor(n)
                   ).pack(side='right', padx=2, pady=(0, 2))
        cols = ('dir', 'id', 'node', 'ext', 'dlc', 'data', 'count', 'period')
        tree = ttk.Treeview(monf, columns=cols, show='headings', height=14)
        for c, txt, w, stretch in (('dir', 'Dir', 40, False),
                                   ('id', 'ID', 70, False),
                                   ('node', 'Node', 80, False),
                                   ('ext', 'Ext', 36, False),
                                   ('dlc', 'DLC', 36, False),
                                   ('data', 'Data (hex)', 220, True),
                                   ('count', 'Count', 70, False),
                                   ('period', 'Period ms', 80, False)):
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor='w', stretch=stretch)
        vs = ttk.Scrollbar(monf, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='left', fill='y')
        self.tree[idx] = tree

    # ── dark theme ──
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use('clam')
        st.configure('.', background=BG, foreground=FG, fieldbackground=FIELD,
                     bordercolor='#444', lightcolor=PANEL, darkcolor=BG)
        st.configure('TFrame', background=BG)
        st.configure('TLabel', background=BG, foreground=FG)
        st.configure('TLabelframe', background=BG, bordercolor='#49494f')
        st.configure('TLabelframe.Label', background=BG, foreground=ACC)
        st.configure('TButton', background=PANEL, foreground=FG, padding=3)
        st.map('TButton', background=[('active', '#3a3a42')])
        st.configure('Small.TButton', background=PANEL, foreground=FG,
                     padding=(4, 0))
        st.map('Small.TButton', background=[('active', '#3a3a42')])
        st.configure('TRadiobutton', background=BG, foreground=FG)
        st.map('TRadiobutton', background=[('active', BG)])
        st.configure('TCombobox', fieldbackground=FIELD, background=PANEL,
                     foreground=FG, arrowcolor=FG)
        st.map('TCombobox', fieldbackground=[('readonly', FIELD)],
               foreground=[('readonly', FG)])
        st.configure('Vertical.TScrollbar', background=PANEL, troughcolor=BG,
                     arrowcolor=FG)
        st.configure('Treeview', background='#141417', fieldbackground='#141417',
                     foreground=FG, rowheight=19, borderwidth=0)
        st.configure('Treeview.Heading', background=PANEL, foreground=ACC,
                     borderwidth=0)
        st.map('Treeview', background=[('selected', '#31313a')])
        st.map('Treeview.Heading', background=[('active', PANEL)])
        for pat, val in (('*TCombobox*Listbox*Background', PANEL),
                         ('*TCombobox*Listbox*Foreground', FG),
                         ('*TCombobox*Listbox*selectBackground', ACC),
                         ('*TCombobox*Listbox*selectForeground', BG)):
            self.root.option_add(pat, val)

    def _help_btn(self, frame, row, col, title, text):
        ttk.Button(frame, text="?", width=2, style='Small.TButton',
                   command=lambda: self._show_help(title, text)
                   ).grid(row=row, column=col, padx=(2, 4))

    def _show_help(self, title, text):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=PANEL)
        win.transient(self.root)
        tk.Label(win, text=title, bg=PANEL, fg=ACC,
                 font=('Segoe UI', 11, 'bold'), justify='left'
                 ).pack(anchor='w', padx=12, pady=(10, 4))
        tk.Label(win, text=text, bg=PANEL, fg=FG, justify='left',
                 wraplength=440, font=('Segoe UI', 9)
                 ).pack(anchor='w', padx=12, pady=4)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=8)
        win.bind('<Escape>', lambda e: win.destroy())

    def _show_about(self):
        win = tk.Toplevel(self.root)
        win.title("About")
        win.configure(bg=PANEL)
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text="CAN Bridge + Logger", bg=PANEL, fg=ACC,
                 font=('Segoe UI', 12, 'bold'), justify='center'
                 ).pack(padx=16, pady=(12, 2))
        tk.Label(win, text=(
            "Bridges two independent CAN adapters (PCAN or SLCAN/CANable, "
            "either side) both ways, with a live per-adapter data list, "
            "optional DBC sender-node lookup, and PCAN-Explorer v2.1 .trc "
            "trace logging."
        ), bg=PANEL, fg=FG, justify='center', wraplength=340,
                 font=('Segoe UI', 9)).pack(padx=16, pady=4)
        tk.Label(win, text=(
            f"Author: {APP_AUTHOR}\n"
            f"Revision: {APP_REVISION}\n"
            f"Date: {APP_REV_DATE}\n"
            f"{APP_WEBSITE}"
        ), bg=PANEL, fg=FG, justify='center', font=('Segoe UI', 9)
                 ).pack(padx=16, pady=(6, 10))
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))
        win.bind('<Escape>', lambda e: win.destroy())

    # ── actions ──
    def _load_dbc(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Load DBC file(s)",
            filetypes=[("DBC files", "*.dbc"), ("All files", "*.*")])
        if not paths:
            return
        self.dbc_nodes = load_dbc_tx_nodes(paths)
        names = ', '.join(os.path.basename(p) for p in paths)
        self.dbc_lbl.config(text=names, foreground=OK)
        self.logq.put(f"DBC loaded: {names} ({len(self.dbc_nodes)} IDs)")

    def toggle_conn(self, idx):
        ch = self.core.ch[idx]
        ui = self.ch_ui[idx]
        if idx in self.pending:
            # a connect attempt is in flight (e.g. a dead SLCAN port that
            # Windows is taking forever to fail on) - there's no way to
            # actually abort that blocked OS call from here, so just detach
            # it: hand the row back to the user now, and if the attempt
            # does eventually resolve in the background, _connect_done
            # will notice it's stale and quietly close it instead of
            # touching the UI.
            del self.pending[idx]
            ui['btn'].config(text="Connect")
            ui['lbl'].config(text="not connected", foreground=ERR)
            self.logq.put(f"CAN{idx}: connect cancelled - still finishing "
                          "in the background, will auto-close if it does "
                          "connect")
            return
        if ch.connected:
            if self.core.bridge_on:
                self.core.set_bridge(False)
                self.btn_br.config(text="Start bridge")
            ch.disconnect()
            ui['btn'].config(text="Connect")
            ui['lbl'].config(text="not connected", foreground=ERR)
            return
        adapter = ui['type'].get()
        channel_name = ui['chan'].get().strip()
        if not channel_name:
            self.logq.put(f"CAN{idx}: no channel/port selected")
            return
        try:
            bitrate = int(ui['rate'].get())
        except ValueError:
            self.logq.put(f"CAN{idx}: invalid bitrate")
            return
        other = self.core.ch[3 - idx]
        other_ui = self.ch_ui[3 - idx]
        if (other.connected and other_ui['type'].get() == adapter
                and other_ui['chan'].get().strip().lower() == channel_name.lower()):
            self.logq.put(f"CAN{idx}: {adapter} {channel_name} already in "
                          f"use by CAN{3 - idx}")
            return
        if not CAN_AVAILABLE:
            self.logq.put("python-can not installed - pip install python-can")
            return
        # opening the adapter (esp. a SLCAN port that isn't actually there)
        # can block for a long time at the OS level - do it off the GUI
        # thread so the window doesn't freeze while it waits. The button
        # stays clickable as a Cancel while this is in flight.
        gen = self.conn_gen[idx] = self.conn_gen[idx] + 1
        self.pending[idx] = gen
        ui['btn'].config(state='normal', text="Cancel")
        ui['lbl'].config(text="connecting...", foreground=FG_DIM)
        threading.Thread(target=self._connect_worker,
                         args=(idx, adapter, channel_name, bitrate, gen),
                         daemon=True).start()

    def _connect_worker(self, idx, adapter, channel_name, bitrate, gen):
        ch = self.core.ch[idx]
        try:
            ch.connect(adapter, channel_name, bitrate)
        except Exception as exc:
            self.root.after(0, self._connect_done, idx, adapter, channel_name,
                            bitrate, exc, gen)
            return
        self.root.after(0, self._connect_done, idx, adapter, channel_name,
                        bitrate, None, gen)

    def _connect_done(self, idx, adapter, channel_name, bitrate, exc, gen):
        if self.pending.get(idx) != gen:
            # cancelled (or superseded by a newer attempt) before this one
            # finished - the UI has already moved on, so just clean up
            # quietly rather than clobbering whatever it's doing now.
            if exc is None:
                self.core.ch[idx].disconnect()
                self.logq.put(f"CAN{idx}: cancelled connect to {channel_name} "
                              "came through after all - closed it")
            return
        del self.pending[idx]
        ui = self.ch_ui[idx]
        ui['btn'].config(state='normal')
        if exc is not None:
            self.logq.put(f"CAN{idx} connect failed: {exc}")
            ui['btn'].config(text="Connect")
            ui['lbl'].config(text="not connected", foreground=ERR)
            return
        ui['btn'].config(text="Disconnect")
        ui['lbl'].config(text=f"{adapter} {channel_name} @ {bitrate}",
                         foreground=OK)

    def toggle_bridge(self):
        if self.core.bridge_on:
            self.core.set_bridge(False)
            self.btn_br.config(text="Start bridge")
        else:
            if self.core.set_bridge(True):
                self.btn_br.config(text="Stop bridge")

    def toggle_logging(self):
        if self.core.trc is not None:
            self.core.stop_logging()
            self.btn_log.config(text="Start logging")
            self.log_lbl.config(text="not logging", foreground=FG_DIM)
        else:
            mode = self.logmode.get()
            direction = self.logdir.get()
            folder = default_trace_folder()
            os.makedirs(folder, exist_ok=True)
            chosen = filedialog.asksaveasfilename(
                parent=self.root, title="Save trace log as",
                initialdir=folder,
                initialfile=default_trace_filename(mode, direction),
                defaultextension=".trc",
                filetypes=[("PCAN Trace", "*.trc"), ("All files", "*.*")])
            if not chosen:
                return          # cancelled - don't start logging
            path = self.core.start_logging(mode, direction, path=chosen)
            if path:
                self.btn_log.config(text="Stop logging")
                self.log_lbl.config(text=os.path.basename(path), foreground=OK)

    def _clear_monitor(self, idx):
        with self.core.mon_lock:
            self.core.mon[idx].clear()
        self.tree[idx].delete(*self.tree[idx].get_children())
        self._mon_sorted[idx].clear()
        self.logq.put(f"CAN{idx} live data cleared")

    def _update_monitor(self, idx):
        tree = self.tree[idx]
        with self.core.mon_lock:
            snap = {k: tuple(v) for k, v in self.core.mon[idx].items()}
        for (direction, aid), (cnt, _t, period, data, extended) in snap.items():
            iid = f"{direction}{aid:X}"
            id_txt = f"0x{aid:08X}" if extended else f"0x{aid:04X}"
            vals = (direction, id_txt, self.dbc_nodes.get(aid, ''),
                    'Y' if extended else '', len(data),
                    data.hex(' ').upper(), cnt,
                    f"{period:.0f}" if period else "-")
            if tree.exists(iid):
                tree.item(iid, values=vals)
            else:
                srt = (aid, direction)
                pos = bisect.bisect_left(self._mon_sorted[idx], srt)
                self._mon_sorted[idx].insert(pos, srt)
                tree.insert('', pos, iid=iid, values=vals)

    def _poll(self):
        while not self.logq.empty():
            line = self.logq.get_nowait()
            stamp = time.strftime('%H:%M:%S')
            self.logbox.config(state='normal')
            self.logbox.insert('end', stamp + ' ' + line + '\n')
            self.logbox.see('end')
            self.logbox.config(state='disabled')
            self._app_log_f.write(stamp + ' ' + line + '\n')
            self._app_log_f.flush()
        self._poll_n += 1
        if self._poll_n % 2 == 0:         # refresh live data at ~5 Hz
            self._update_monitor(1)
            self._update_monitor(2)
        c1, c2 = self.core.ch[1], self.core.ch[2]
        trc = self.core.trc
        self.stats.config(text=(
            f"CAN1  TX {c1.tx_count}  RX {c1.rx_count}      "
            f"CAN2  TX {c2.tx_count}  RX {c2.rx_count}      "
            f"bridge {'RUNNING' if self.core.bridge_on else 'off'}   "
            f"log {f'{trc.count} frames' if trc else 'off'}"))
        self.lbl_br.config(
            text='bridge RUNNING' if self.core.bridge_on else 'bridge off',
            foreground=OK if self.core.bridge_on else FG_DIM)
        self.root.after(100, self._poll)


if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    finally:
        app.core.shutdown()
        app._app_log_f.close()
