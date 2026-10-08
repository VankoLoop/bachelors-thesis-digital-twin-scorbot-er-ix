#!/usr/bin/env python3
"""
Program 1 - COM0 STREAM listener  (Windows 11 / Python 3)

Listens on the port where the ACL STREAM program emits (PRLNCOM 0 ...).

Console shows ENCODER COUNTS:      Q1=6844 Q2=-1203 Q3=500 Q4=0 Q5=0 G=1
positions.txt shows 1/100 DEGREE:  Q1=562 Q2=-87 Q3=41 Q4=0 Q5=0 G=1   (100 = 1 deg)
Gripper G is the raw output bit 0/1.

Accepts BOTH STREAM formats:
  compact  (default):  one line  "e1 e2 e3 e4 e5 g"   (gripper is the 6th value)
  labelled (fallback): "G <bit>" then "A1 <enc1>" ... "A5 <enc5>"
Pure listener - STREAM runs on the controller.

Link: 9600 8N1, XON/XOFF.   Setup: pip install pyserial
Run:  python listen.py                  Stop: Ctrl+C
"""

import os
import re
import time
import serial                      # pip install pyserial

# --------------------------- CONFIG (edit) --------------------------
PORT               = "COM3"        # USB-serial adapter on the COM0 stream (Device Manager)
BAUD               = 9600
N_AXES             = 5
GRIPPER_OUT_CLOSED = 1             # output bit that means CLOSED (only used if STREAM
                                   # sends the word form "G OPEN"/"G CLOSED")
OUT_FILE           = "positions.txt"   # persistent pose file, updated IN PLACE (never emptied/renamed)
CONSOLE_EVERY      = 0.1               # seconds; console prints a changed pose at most this often

# Encoder counts per degree - from Resources/Encoder counts per degree.xlsx
COUNTS_PER_DEG = [1216.6477, 1385.2272, 1212.1022, 1022.7272, 568.1818]
# NOTE: axes 4 & 5 are a differential wrist; these are per-motor factors. For true
# pitch/roll you may still need the 4/5 coupling - ask if you want it added.
# --------------------------------------------------------------------

TAG_RE  = re.compile(r"^\s*A(\d)\s+(-?\d+)")   # "A1 6844"
GRIP_RE = re.compile(r"^\s*G\s+(\S+)", re.I)   # "G 1" or "G OPEN"
INT_RE  = re.compile(r"-?\d+")


def win_port(name):
    m = re.match(r"^COM(\d+)$", name, re.I)
    return r"\\.\%s" % name if m and int(m.group(1)) >= 10 else name


def read_available(ser):
    n = ser.in_waiting if hasattr(ser, "in_waiting") else ser.inWaiting()
    return ser.read(n) if n else ser.read(1)


def grip_bit(token):
    """Return 0/1 from a raw bit ('0'/'1') or the word form ('OPEN'/'CLOSED')."""
    t = token.strip().upper()
    if t == "CLOSED":
        return GRIPPER_OUT_CLOSED
    if t == "OPEN":
        return 1 - GRIPPER_OUT_CLOSED
    try:
        return int(t)
    except ValueError:
        return None


def to_centideg(counts):
    """Encoder counts -> hundredths of a degree (100 = 1 deg), rounded to whole."""
    return [int(round(c * 100.0 / COUNTS_PER_DEG[i])) for i, c in enumerate(counts)]


def fmt(vals, g):
    return " ".join("Q%d=%d" % (i + 1, vals[i]) for i in range(N_AXES)) + " G=%d" % g


_skipped = [0]   # count of frames dropped because the file was momentarily locked


def update_in_place(path, line):
    """Update positions.txt IN PLACE - the way the SolidWorks API needs it.

    SolidWorks keeps positions.txt open and does NOT tolerate the file being
    renamed/replaced, and it must always find a complete "Q1=.. .. G=.." line.
    So we never rename and never empty the file:

      - open "r+"  (does NOT truncate on open, unlike "w" which blanks it first --
        that blanking was the empty-file race that crashed things)
      - seek(0), overwrite the line from the top
      - truncate() to the new length, to drop any tail left from a longer line

    The file is therefore only ever UPDATED, never emptied, never renamed. If the
    reader has it locked for an instant, retry briefly, then skip this frame
    rather than crash; the next pose (~tens of ms later) tries again.
    """
    for _ in range(30):                           # ~30 ms budget of 1 ms retries
        try:
            if os.path.exists(path):
                f = open(path, "r+")              # existing file, no truncate-on-open
            else:
                f = open(path, "w")               # first run: create it
            try:
                f.seek(0)
                f.write(line)
                f.truncate()                      # cut leftover if new line is shorter
                f.flush()
            finally:
                f.close()
            return True
        except (IOError, OSError):                # momentary lock -> retry
            time.sleep(0.001)
    _skipped[0] += 1                              # give up on THIS frame, don't crash
    if _skipped[0] % 50 == 1:
        print("  [warn] positions.txt locked -> dropped %d frame(s)." % _skipped[0])
    return False


_last_file  = [None]   # last line written to positions.txt; skip identical rewrites
_last_print = [0.0]    # time of last console print (CONSOLE_EVERY throttle)


def write_pose(counts, g):
    file_line = fmt(to_centideg(counts), g)       # 1/100 degree
    if file_line == _last_file[0]:                # pose unchanged -> nothing to do
        return
    if update_in_place(OUT_FILE, file_line + "\n"):   # write only on change (mark if it stuck)
        _last_file[0] = file_line
    now = time.time()                             # console: on change, at most every CONSOLE_EVERY s
    if now - _last_print[0] >= CONSOLE_EVERY:
        print(fmt(counts, g))                     # encoder counts
        _last_print[0] = now


def main():
    print("Opening %s at %d 8N1 XON/XOFF ..." % (PORT, BAUD))
    ser = serial.Serial(win_port(PORT), BAUD, bytesize=8, parity="N",
                        stopbits=1, xonxoff=False, timeout=1)
    print("Listening for STREAM (gripper + 5 encoders).  Ctrl+C to stop.\n")

    update_in_place(OUT_FILE, fmt([0] * N_AXES, 0) + "\n")   # seed a valid line before first cycle

    axes, grip, buf = {}, 0, ""
    try:
        while True:
            chunk = read_available(ser)
            if not chunk:
                continue
            buf += chunk.decode("latin-1", "ignore")
            parts = re.split(r"[\r\n]+", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.strip()
                if not line:
                    continue
                m = TAG_RE.match(line)            # axis line "A1 6844"
                if m:
                    axes[int(m.group(1))] = int(m.group(2))
                    if all(i in axes for i in range(1, N_AXES + 1)):
                        write_pose([axes[i] for i in range(1, N_AXES + 1)], grip)
                        axes.clear()
                    continue
                g = GRIP_RE.match(line)           # gripper line "G 1"
                if g:
                    b = grip_bit(g.group(1))
                    if b is not None:
                        grip = b
                    continue
                ints = [int(x) for x in INT_RE.findall(line)]   # compact one line: "e1 e2 e3 e4 e5 g"
                if len(ints) >= N_AXES + 1:                      # 6th value is the gripper bit
                    write_pose(ints[:N_AXES], ints[N_AXES])
                elif len(ints) >= N_AXES:                        # 5 values only: keep last gripper
                    write_pose(ints[:N_AXES], grip)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
