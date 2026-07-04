#!/usr/bin/env python3
# time-machine — a clock-drift auditor.
# MIT License. Single file, stdlib only.
"""
time-machine: audit how far your system clock sits from the rest of the world,
and render one instant across every major time scale.

An honest note on framing: there is no single live master clock. UTC is a
post-hoc committee construction — the BIPM averages a few hundred atomic
clocks and publishes the result (Circular T) weeks after the fact. What you
can actually reach over a network are local *realizations* of UTC, such as
UTC(NIST) or UTC(USNO), each steered toward that paper average. This tool
measures your clock against several such realizations, reports how much they
agree with each other, and flags sources that disagree with the consensus.
"True time" is a negotiation, not an oracle. This tool audits the negotiation.
"""

import argparse
import datetime as dt
import json
import math
import re
import socket
import statistics
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# Leap-second constants.
#
# These two offsets change ONLY when the IERS publishes a Bulletin C
# announcing a leap second (checked twice a year, June/December), and they
# always bump together: TAI-GPS is frozen at 19 s forever (GPS time matched
# TAI-19 at the GPS epoch in 1980 and never leaps), so
#     GPS_MINUS_UTC == TAI_MINUS_UTC - 19   at all times.
# Last change: 2017-01-01 (Bulletin C 52). If a new leap second is announced,
# bump TAI_MINUS_UTC and GPS_MINUS_UTC follows automatically.
# --------------------------------------------------------------------------
TAI_MINUS_UTC = 37                      # seconds, TAI - UTC
GPS_MINUS_UTC = TAI_MINUS_UTC - 19      # seconds, GPS - UTC (= 18)

# Fixed epoch arithmetic (these never change).
NTP_UNIX_DELTA = 2_208_988_800          # seconds between 1900-01-01 and 1970-01-01
GPS_EPOCH_UNIX = 315_964_800            # 1980-01-06T00:00:00Z as Unix seconds
JD_UNIX_EPOCH = 2_440_587.5             # Julian Date of the Unix epoch
MJD_OFFSET = 2_400_000.5                # JD - MJD
SECS_PER_WEEK = 604_800

DEFAULT_SERVERS = [
    "time.nist.gov",
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
]

HONESTY = (
    "UTC is a post-hoc committee construction (BIPM Circular T, published weeks "
    "late). There is no single live master clock — only local realizations like "
    "UTC(NIST), steered toward a paper average. This report audits your clock "
    "against several such realizations and their mutual (dis)agreement."
)

# Named instants you can --at yourself to. Time travel, terminal edition.
NAMED_INSTANTS = {
    "now": None,  # resolved at call time
    "unix-epoch": 0.0,
    "ntp-epoch": float(-NTP_UNIX_DELTA),
    "gps-epoch": float(GPS_EPOCH_UNIX),
    "mjd-epoch": -3_506_716_800.0,           # 1858-11-17T00:00:00Z
    "moon-landing": -14_182_940.0,           # 1969-07-20T20:17:40Z, Eagle has landed
    "y2k": 946_684_800.0,                    # 2000-01-01T00:00:00Z
    "bttf": 1_445_444_940.0,                 # 2015-10-21T16:29:00Z, Doc's destination
    "2038": 2_147_483_647.0,                 # int32 time_t doom
    "ntp-rollover": float((1 << 32) - NTP_UNIX_DELTA),  # 2036-02-07, NTP era 0 ends
}

# --------------------------------------------------------------------------
# Terminal color helpers
# --------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


def green(t):  return c(t, "32")
def yellow(t): return c(t, "33")
def red(t):    return c(t, "31")
def dim(t):    return c(t, "2")
def bold(t):   return c(t, "1")
def cyan(t):   return c(t, "36")


# --------------------------------------------------------------------------
# Timestamp conversions
# --------------------------------------------------------------------------
def unix_to_ntp64(unix_s):
    """Unix seconds (float) -> 64-bit NTP timestamp (32.32 fixed point)."""
    ntp = unix_s + NTP_UNIX_DELTA
    sec = int(ntp)
    frac = int(round((ntp - sec) * (1 << 32)))
    if frac == (1 << 32):
        sec, frac = sec + 1, 0
    return ((sec & 0xFFFFFFFF) << 32) | frac


def ntp64_to_unix(value):
    """64-bit NTP timestamp -> Unix seconds (float). Assumes NTP era 0."""
    sec = value >> 32
    frac = value & 0xFFFFFFFF
    return sec + frac / (1 << 32) - NTP_UNIX_DELTA


def offset_delay(t1, t2, t3, t4):
    """Classic NTP clock offset and round-trip delay from the four timestamps.

    t1: client transmit, t2: server receive, t3: server transmit,
    t4: client receive (t1/t4 on the local clock, t2/t3 on the server clock).
    """
    offset = ((t2 - t1) + (t3 - t4)) / 2.0
    delay = (t4 - t1) - (t3 - t2)
    return offset, delay


def gps_week_tow(unix_s, leap=None):
    """Unix seconds -> (GPS week number, time-of-week seconds).

    GPS time runs ahead of UTC by the leap-second count accrued since 1980
    (currently GPS_MINUS_UTC). Pass leap=0 to treat the input as already
    being on the GPS time scale (used by the self-tests).
    """
    if leap is None:
        leap = GPS_MINUS_UTC
    gps_seconds = (unix_s - GPS_EPOCH_UNIX) + leap
    week = math.floor(gps_seconds / SECS_PER_WEEK)
    tow = gps_seconds - week * SECS_PER_WEEK
    return int(week), tow


def julian_date(unix_s):
    return unix_s / 86400.0 + JD_UNIX_EPOCH


def gmst_hours(jd):
    """Greenwich Mean Sidereal Time in hours (IAU 1982-ish, plenty for display)."""
    t = (jd - 2451545.0) / 36525.0
    deg = (280.46061837
           + 360.98564736629 * (jd - 2451545.0)
           + 0.000387933 * t * t
           - t * t * t / 38710000.0)
    return (deg % 360.0) / 15.0


def swatch_beats(utc):
    """Swatch Internet Time: the day sliced into 1000 .beats on UTC+1 (BMT)."""
    bmt = utc + dt.timedelta(hours=1)
    day_seconds = bmt.hour * 3600 + bmt.minute * 60 + bmt.second + bmt.microsecond / 1e6
    return day_seconds / 86.4


# --------------------------------------------------------------------------
# SNTP client (RFC 4330), hand-rolled over UDP/123
# --------------------------------------------------------------------------
def split_host_port(server, default_port=123):
    """'host', 'host:port', or '[v6]:port' -> (host, port).

    Bare IPv6 literals (more than one colon, no brackets) stay intact.
    """
    if server.startswith("["):
        host, _, rest = server[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else default_port
    if server.count(":") == 1:
        host, port = server.split(":")
        if port.isdigit():
            return host, int(port)
    return server, default_port


def sntp_query(server, timeout):
    """One SNTP exchange. Returns a result dict; raises on failure."""
    host, port = split_host_port(server)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    family, socktype, proto, _, addr = infos[0]

    packet = bytearray(48)
    packet[0] = (0 << 6) | (4 << 3) | 3   # LI=0, VN=4, Mode=3 (client)

    with socket.socket(family, socktype, proto) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        t1_ntp = unix_to_ntp64(t1)
        struct.pack_into("!Q", packet, 40, t1_ntp)  # transmit timestamp
        sock.sendto(bytes(packet), addr)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("timed out")
            sock.settimeout(remaining)
            data, _peer = sock.recvfrom(512)
            t4 = time.time()
            if len(data) >= 48:
                break

    mode = data[0] & 0x07
    leap = (data[0] >> 6) & 0x03
    stratum = data[1]
    if mode not in (4, 5):
        raise ValueError(f"unexpected mode {mode} in reply")
    if stratum == 0:
        kod = data[12:16].decode("ascii", "replace")
        raise ValueError(f"kiss-of-death from server ({kod!r})")
    origin = struct.unpack_from("!Q", data, 24)[0]
    if origin != t1_ntp:
        raise ValueError("origin timestamp mismatch (bogus/spoofed reply)")

    t2 = ntp64_to_unix(struct.unpack_from("!Q", data, 32)[0])
    t3 = ntp64_to_unix(struct.unpack_from("!Q", data, 40)[0])
    offset, delay = offset_delay(t1, t2, t3, t4)

    refid = data[12:16]
    ref = refid.decode("ascii") if stratum == 1 and all(
        32 <= b < 127 or b == 0 for b in refid) else ".".join(str(b) for b in refid)

    return {
        "server": server,
        "address": addr[0],
        "stratum": stratum,
        "leap_indicator": leap,
        "refid": ref.strip("\x00"),
        "offset": offset,     # seconds; positive = local clock is behind
        "delay": delay,       # seconds, round trip
        "t1": t1, "t2": t2, "t3": t3, "t4": t4,
    }


def query_server(server, timeout, samples):
    """Query one server `samples` times; keep the lowest-delay exchange."""
    best = None
    last_err = None
    for _ in range(samples):
        try:
            r = sntp_query(server, timeout)
            if best is None or r["delay"] < best["delay"]:
                best = r
        except (OSError, ValueError) as exc:
            last_err = exc
    if best is not None:
        return best
    return {"server": server, "error": f"{type(last_err).__name__}: {last_err}"}


def poll_servers(servers, timeout, samples):
    with ThreadPoolExecutor(max_workers=max(len(servers), 1)) as pool:
        return list(pool.map(lambda s: query_server(s, timeout, samples), servers))


def build_consensus(results, outlier_ms):
    """Pick the best source and characterize cross-source disagreement."""
    ok = [r for r in results if "error" not in r]
    if not ok:
        return None
    offsets = [r["offset"] for r in ok]
    median = statistics.median(offsets)
    spread = (max(offsets) - min(offsets)) if len(ok) > 1 else 0.0
    threshold = outlier_ms / 1000.0
    for r in ok:
        r["outlier"] = abs(r["offset"] - median) > threshold
    trusted = [r for r in ok if not r["outlier"]] or ok
    best = min(trusted, key=lambda r: r["delay"])
    return {
        "best": best,
        "offset": best["offset"],
        "delay": best["delay"],
        "median_offset": median,
        "spread": spread,
        "sources_ok": len(ok),
        "sources_total": len(results),
        "outliers": [r["server"] for r in ok if r["outlier"]],
    }


# --------------------------------------------------------------------------
# gpsd cross-check (optional; pure stdlib socket + json)
# --------------------------------------------------------------------------
def gpsd_query(host="127.0.0.1", port=2947, timeout=3.0):
    """Ask a local gpsd for satellite-derived time via its JSON protocol.

    Returns (status, data): status is "ok", "nofix", or "unavailable".
    Note: TPV time over NMEA is typically only good to ~0.1-0.5 s unless
    gpsd is fed PPS; treat the delta as a coarse cross-check, not a ruler.
    """
    try:
        sock = socket.create_connection((host, port), timeout=min(timeout, 2.0))
    except OSError:
        return "unavailable", None

    with sock:
        try:
            sock.settimeout(0.5)
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buf = b""
            deadline = time.monotonic() + timeout
            saw_tpv = False
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    local = time.time()
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") != "TPV":
                        continue
                    saw_tpv = True
                    if msg.get("mode", 0) >= 2 and "time" in msg:
                        iso = msg["time"].replace("Z", "+00:00")
                        gps_unix = dt.datetime.fromisoformat(iso).timestamp()
                        return "ok", {
                            "time_unix": gps_unix,
                            "local_unix": local,
                            "offset": gps_unix - local,  # same sign convention as NTP
                            "fix_mode": msg.get("mode"),
                            "device": msg.get("device", ""),
                        }
        except OSError:
            return "unavailable", None
    return ("nofix", None) if saw_tpv else ("nofix", None)


# --------------------------------------------------------------------------
# Time scales
# --------------------------------------------------------------------------
def scales_for(unix_s):
    """Render one instant (Unix seconds, UTC-based) across the major scales."""
    utc = dt.datetime.fromtimestamp(unix_s, dt.timezone.utc)
    tai = utc + dt.timedelta(seconds=TAI_MINUS_UTC)
    week, tow = gps_week_tow(unix_s)
    jd = julian_date(unix_s)
    mjd = jd - MJD_OFFSET
    ntp64 = unix_to_ntp64(unix_s)
    beats = swatch_beats(utc)
    iso_year, iso_week, iso_day = utc.isocalendar()
    scales = {
        "utc": utc.strftime("%Y-%m-%d %H:%M:%S.%f") + " UTC",
        "tai": tai.strftime("%Y-%m-%d %H:%M:%S.%f") + f" TAI (UTC+{TAI_MINUS_UTC}s)",
        "gps_week": week,
        "gps_tow": tow,
        "gps_week_mod1024": week % 1024,
        "unix_s": unix_s,
        "unix_ns": int(round(unix_s * 1e9)),
        "ntp_s": unix_s + NTP_UNIX_DELTA,
        "ntp_hex": f"0x{ntp64 >> 32:08X}.{ntp64 & 0xFFFFFFFF:08X}",
        "jd": jd,
        "mjd": mjd,
        "gmst_hours": gmst_hours(jd),
        "beats": beats,
        "day_of_year": int(utc.strftime("%j")),
        "iso_week": f"{iso_year}-W{iso_week:02d}-{iso_day}",
    }
    try:
        local = dt.datetime.fromtimestamp(unix_s).astimezone()
        scales["local"] = local.strftime("%Y-%m-%d %H:%M:%S.%f %Z (UTC%z)")
    except (OverflowError, OSError, ValueError):
        scales["local"] = None
    return scales


def hours_to_hms(hours):
    total = hours * 3600.0
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:04.1f}"


def render_scales(scales, indent="  "):
    lines = []

    def row(label, value):
        lines.append(f"{indent}{cyan(f'{label:<9}')} {value}")

    row("UTC", bold(scales["utc"]))
    if scales.get("local"):
        row("Local", scales["local"])
    row("TAI", scales["tai"])
    row("GPS", f"week {scales['gps_week']}, tow {scales['gps_tow']:.6f} s "
               f"(UTC+{GPS_MINUS_UTC}s; mod-1024 week {scales['gps_week_mod1024']})")
    row("Unix", f"{scales['unix_s']:.6f} s  |  {scales['unix_ns']} ns")
    row("NTP", f"{scales['ntp_s']:.6f} s since 1900  |  {scales['ntp_hex']}")
    row("Julian", f"JD {scales['jd']:.6f}  ·  MJD {scales['mjd']:.6f}")
    row("Sidereal", f"GMST {hours_to_hms(scales['gmst_hours'])}")
    row("Swatch", f"@{scales['beats']:06.2f} .beats (BMT, UTC+1)")
    row("Calendar", f"day {scales['day_of_year']} of year · ISO {scales['iso_week']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def fmt_ms(seconds, signed=True):
    ms = seconds * 1000.0
    return f"{ms:+.3f} ms" if signed else f"{ms:.3f} ms"


def colored_offset(seconds):
    text = fmt_ms(seconds)
    ms = abs(seconds * 1000.0)
    if ms < 25:
        return green(text)
    if ms < 250:
        return yellow(text)
    return red(text)


def render_report(results, consensus, gps_status, gps_data, scales, args):
    out = []
    out.append(bold("── time-machine ─ local clock drift audit " + "─" * 20))
    out.append("")
    out.append(bold("Sources") + dim("  (offset >0 means your clock is behind)"))
    width = max(len(r["server"]) for r in results)
    for r in results:
        name = f"{r['server']:<{width}}"
        if "error" in r:
            out.append(f"  {name}  {red('unreachable')}  {dim(r['error'])}")
            continue
        flag = red("  ⚠ OUTLIER") if r.get("outlier") else ""
        out.append(
            f"  {name}  {dim(r['address']):<17}  s{r['stratum']}  "
            f"offset {colored_offset(r['offset'])}  "
            f"delay {fmt_ms(r['delay'], signed=False)}{flag}"
        )
    out.append("")

    if consensus is None:
        out.append(red("No time source reachable — cannot audit the clock."))
        return "\n".join(out)

    out.append(bold("Consensus"))
    out.append(f"  best source     {consensus['best']['server']} "
               f"{dim('(lowest round-trip delay among agreeing sources)')}")
    out.append(f"  clock offset    {colored_offset(consensus['offset'])}   "
               f"{dim('median across sources:')} {fmt_ms(consensus['median_offset'])}")
    spread_txt = fmt_ms(consensus["spread"], signed=False)
    spread_col = green(spread_txt) if consensus["spread"] * 1000 < args.outlier_ms \
        else yellow(spread_txt)
    out.append(f"  source spread   {spread_col} "
               f"{dim('(max pairwise offset disagreement)')}")
    if consensus["outliers"]:
        out.append(red(f"  ⚠ outliers      {', '.join(consensus['outliers'])} "
                       f"deviate > {args.outlier_ms:g} ms from the median — "
                       f"possibly lying or misconfigured"))
    out.append("")

    out.append(bold("Satellite cross-check"))
    if gps_status == "ok":
        delta = gps_data["offset"] - consensus["offset"]
        out.append(f"  gpsd fix (mode {gps_data['fix_mode']}): GPS-derived offset "
                   f"{fmt_ms(gps_data['offset'])}; GPS vs NTP consensus: "
                   f"{colored_offset(delta)} {dim('(coarse without PPS)')}")
    elif gps_status == "nofix":
        out.append(dim("  gpsd is running but has no usable fix yet — "
                       "satellite cross-check skipped."))
    else:
        out.append(dim("  satellite cross-check unavailable (no gpsd on "
                       f"{args.gpsd_host}:{args.gpsd_port}) — continuing on NTP alone."))
    out.append("")

    out.append(bold("The consensus instant, across the scales"))
    out.append(render_scales(scales))
    out.append("")
    out.append(dim("  " + HONESTY))
    return "\n".join(out)


def json_report(results, consensus, gps_status, gps_data, scales, watch_stats=None):
    doc = {
        "kind": "time-machine.report",
        "version": __version__,
        "servers": [
            {k: v for k, v in r.items() if k not in ("t1", "t2", "t3", "t4")}
            for r in results
        ],
        "consensus": None,
        "gps": {"status": gps_status, "data": gps_data},
        "scales": scales,
        "honesty": HONESTY,
    }
    if consensus is not None:
        doc["consensus"] = {
            "best_server": consensus["best"]["server"],
            "offset_s": consensus["offset"],
            "delay_s": consensus["delay"],
            "median_offset_s": consensus["median_offset"],
            "spread_s": consensus["spread"],
            "sources_ok": consensus["sources_ok"],
            "sources_total": consensus["sources_total"],
            "outliers": consensus["outliers"],
        }
    if watch_stats is not None:
        doc["watch"] = watch_stats
    return json.dumps(doc, default=str)


# --------------------------------------------------------------------------
# Watch mode: drift tracking with linear regression on the monotonic axis
# --------------------------------------------------------------------------
def linreg_slope(points):
    """Least-squares slope for [(x, y), ...]; returns 0.0 if degenerate."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    denom = sum((p[0] - mx) ** 2 for p in points)
    if denom == 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in points) / denom


def watch_stats(samples):
    """samples: [(monotonic_s, offset_s), ...] — offsets vs time.monotonic().

    The x-axis is deliberately time.monotonic(): if something (or someone)
    steps the system clock mid-session, a wall-clock x-axis would poison the
    regression; the monotonic clock can't be stepped.
    """
    offsets = [o for _, o in samples]
    stats = {
        "n": len(samples),
        "min_offset_s": min(offsets),
        "max_offset_s": max(offsets),
        "mean_offset_s": sum(offsets) / len(offsets),
        "span_s": samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0,
        "drift_ppm": None,
    }
    if len(samples) >= 3 and stats["span_s"] > 0:
        stats["drift_ppm"] = linreg_slope(samples) * 1e6
    if len(offsets) >= 2:
        stats["stdev_offset_s"] = statistics.stdev(offsets)
    return stats


def run_watch(args):
    print(dim(f"time-machine watch: polling {len(args.server)} server(s) every "
              f"{args.watch:g} s — Ctrl-C for the session verdict."), file=sys.stderr)
    samples = []
    worst_exit = 0
    try:
        while True:
            started = time.monotonic()
            results = poll_servers(args.server, args.timeout, args.samples)
            consensus = build_consensus(results, args.outlier_ms)
            now_mono = time.monotonic()
            if consensus is not None:
                samples.append((now_mono, consensus["offset"]))
                stats = watch_stats(samples)
                if consensus["outliers"]:
                    worst_exit = 2
                if args.json:
                    scales = scales_for(time.time() + consensus["offset"])
                    print(json_report(results, consensus, "skipped", None,
                                      scales, stats), flush=True)
                else:
                    stamp = dt.datetime.now().strftime("%H:%M:%S")
                    drift = (f"drift {stats['drift_ppm']:+.2f} ppm"
                             if stats["drift_ppm"] is not None else "drift n/a")
                    outl = (red(f" ⚠ {','.join(consensus['outliers'])}")
                            if consensus["outliers"] else "")
                    print(f"{dim(stamp)}  offset {colored_offset(consensus['offset'])}  "
                          f"delay {fmt_ms(consensus['delay'], signed=False)}  "
                          f"spread {fmt_ms(consensus['spread'], signed=False)}  "
                          f"src {consensus['best']['server']}  "
                          f"[{consensus['sources_ok']}/{consensus['sources_total']}]  "
                          f"{dim(drift)}{outl}", flush=True)
            else:
                worst_exit = max(worst_exit, 1)
                msg = "all sources unreachable this round"
                if args.json:
                    print(json.dumps({"kind": "time-machine.watch-error",
                                      "error": msg}), flush=True)
                else:
                    print(red(msg), flush=True)
            time.sleep(max(0.0, args.watch - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass

    if samples and not args.json:
        stats = watch_stats(samples)
        print()
        print(bold("Session verdict"))
        print(f"  polls        {stats['n']} over {stats['span_s']:.1f} s")
        print(f"  offset       min {fmt_ms(stats['min_offset_s'])}   "
              f"max {fmt_ms(stats['max_offset_s'])}   "
              f"mean {fmt_ms(stats['mean_offset_s'])}")
        if "stdev_offset_s" in stats:
            print(f"  jitter       stdev {fmt_ms(stats['stdev_offset_s'], signed=False)}")
        if stats["drift_ppm"] is not None:
            note = dim("(linear fit of offset vs time.monotonic(); "
                       "+ means your clock runs slow)")
            print(f"  drift rate   {stats['drift_ppm']:+.3f} ppm {note}")
        print(dim("  " + HONESTY))
    return worst_exit


# --------------------------------------------------------------------------
# Epoch gallery
# --------------------------------------------------------------------------
def run_epochs():
    print(bold("── epoch gallery ─ famous zero-points and cliff edges " + "─" * 10))
    rows = [
        ("MJD epoch", NAMED_INSTANTS["mjd-epoch"], "Modified Julian Date day 0"),
        ("NTP epoch", NAMED_INSTANTS["ntp-epoch"], "NTP era 0 begins"),
        ("Moon landing", NAMED_INSTANTS["moon-landing"], "Eagle has landed"),
        ("Unix epoch", NAMED_INSTANTS["unix-epoch"], "time_t zero"),
        ("GPS epoch", NAMED_INSTANTS["gps-epoch"], "GPS week 0 begins"),
        ("Y2K", NAMED_INSTANTS["y2k"], "the bug that mostly wasn't"),
        ("BTTF day", NAMED_INSTANTS["bttf"], "Doc Brown's destination"),
        ("NTP rollover", NAMED_INSTANTS["ntp-rollover"], "NTP era 0 ends"),
        ("2038 cliff", NAMED_INSTANTS["2038"], "int32 time_t overflows"),
    ]
    print(f"  {'name':<13} {'UTC':<21} {'unix s':>13} {'JD':>14}  {'GPS week':>8}")
    for name, unix_s, note in rows:
        utc = dt.datetime.fromtimestamp(unix_s, dt.timezone.utc)
        week, _ = gps_week_tow(unix_s)
        gweek = str(week) if unix_s >= GPS_EPOCH_UNIX else "—"
        print(f"  {cyan(f'{name:<13}')} {utc.strftime('%Y-%m-%d %H:%M:%S'):<21} "
              f"{unix_s:>13.0f} {julian_date(unix_s):>14.2f}  {gweek:>8}  {dim(note)}")
    print(dim(f"\n  Use --at <name> to render any of these across all scales "
              f"(names: {', '.join(k for k in NAMED_INSTANTS if k != 'now')})."))


# --------------------------------------------------------------------------
# --at parsing: absolute, relative, named — pick your destination
# --------------------------------------------------------------------------
def parse_instant(text):
    """ISO-8601, Unix seconds, relative (+90m/-2d/+1y), or a named instant."""
    text = text.strip()
    if text in NAMED_INSTANTS:
        value = NAMED_INSTANTS[text]
        return time.time() if value is None else value
    m = re.fullmatch(r"([+-])(\d+(?:\.\d+)?)([smhdwy]?)", text)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        mult = {"": 1, "s": 1, "m": 60, "h": 3600,
                "d": 86400, "w": 604800, "y": 31_557_600}[m.group(3)]
        return time.time() + sign * float(m.group(2)) * mult
    try:
        return float(text)
    except ValueError:
        pass
    try:
        d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"can't parse instant {text!r}: use ISO-8601, Unix seconds, "
            f"±N[smhdwy], or one of {', '.join(NAMED_INSTANTS)}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


# --------------------------------------------------------------------------
# Self-tests
# --------------------------------------------------------------------------
def run_selftest():
    failures = 0

    def check(name, condition, detail=""):
        nonlocal failures
        if condition:
            print(f"  {green('PASS')}  {name}")
        else:
            failures += 1
            print(f"  {red('FAIL')}  {name}  {detail}")

    print(bold("time-machine self-test (offline)"))
    check("Julian Date of the Unix epoch is 2440587.5",
          julian_date(0) == 2440587.5, f"got {julian_date(0)!r}")

    week, tow = gps_week_tow(GPS_EPOCH_UNIX, leap=0)
    check("GPS epoch maps to week 0, tow 0",
          week == 0 and abs(tow) < 1e-9, f"got week={week} tow={tow}")

    t = 1_234_567_890.123456
    rt = ntp64_to_unix(unix_to_ntp64(t))
    check("NTP <-> Unix round-trip within 1 µs",
          abs(rt - t) < 1e-6, f"error {abs(rt - t):.3e} s")

    off, dly = offset_delay(100.0, 110.0, 110.5, 101.0)
    check("offset/delay math on synthetic timestamps",
          abs(off - 9.75) < 1e-12 and abs(dly - 0.5) < 1e-12,
          f"got offset={off} delay={dly}")

    check("TAI-GPS pinned at 19 s (leap constants bump together)",
          TAI_MINUS_UTC - GPS_MINUS_UTC == 19)

    mjd0 = scales_for(NAMED_INSTANTS["mjd-epoch"])["mjd"]
    check("MJD epoch (1858-11-17) is MJD 0.0", mjd0 == 0.0, f"got {mjd0!r}")

    beats_mid = swatch_beats(dt.datetime(2020, 1, 1, 23, 0, tzinfo=dt.timezone.utc))
    beats_noon = swatch_beats(dt.datetime(2020, 1, 1, 11, 0, tzinfo=dt.timezone.utc))
    check("Swatch .beats: BMT midnight is @000, BMT noon is @500",
          abs(beats_mid) < 1e-9 and abs(beats_noon - 500.0) < 1e-9,
          f"got {beats_mid}, {beats_noon}")

    gmst_j2000 = gmst_hours(2451545.0)
    check("GMST at J2000 is ~18.697h",
          abs(gmst_j2000 - 18.697374558) < 1e-3, f"got {gmst_j2000}")

    slope = linreg_slope([(0, 0.0), (10, 1e-5), (20, 2e-5)])
    check("drift regression recovers 1 ppm exactly",
          abs(slope * 1e6 - 1.0) < 1e-9, f"got {slope * 1e6} ppm")

    print()
    if failures:
        print(red(f"{failures} check(s) failed"))
        return 1
    print(green("all checks passed — the machinery is sound"))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="time-machine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "A clock-drift auditor. Measures how far the local system clock sits\n"
            "from multiple independent time authorities (SNTP over UDP/123, plus a\n"
            "gpsd satellite cross-check when available) and renders the consensus\n"
            "instant across every major time scale: UTC, TAI, GPS week/tow, Unix,\n"
            "NTP, Julian/Modified Julian Date, sidereal, Swatch .beats."
        ),
        epilog=(
            "honesty:\n"
            f"  {HONESTY}\n\n"
            "exit codes:\n"
            "  0  audit completed, sources agree\n"
            "  1  no usable source / self-test failure\n"
            "  2  at least one source flagged as an outlier (scriptable: alert on it)\n\n"
            "time travel:\n"
            "  --at works offline: ISO-8601 ('2015-10-21T16:29Z'), Unix seconds,\n"
            "  relative jumps ('+90m', '-2d', '+1y'), or named instants\n"
            f"  ({', '.join(k for k in NAMED_INSTANTS if k != 'now')}).\n"
        ),
    )
    p.add_argument("--server", action="append", metavar="HOST",
                   help="NTP server to query; repeatable "
                        f"(default: {', '.join(DEFAULT_SERVERS)})")
    p.add_argument("--timeout", type=float, default=2.0, metavar="SEC",
                   help="per-query UDP timeout in seconds (default: 2)")
    p.add_argument("--samples", type=int, default=1, metavar="N",
                   help="SNTP exchanges per server, best (lowest-delay) kept "
                        "(default: 1)")
    p.add_argument("--outlier-ms", type=float, default=100.0, metavar="MS",
                   help="flag sources deviating more than this from the median "
                        "offset (default: 100)")
    p.add_argument("--watch", type=float, metavar="N",
                   help="re-poll every N seconds; tracks min/max/mean offset and "
                        "drift rate in ppm (offset regressed against "
                        "time.monotonic())")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output (NDJSON in --watch mode)")
    p.add_argument("--at", type=parse_instant, metavar="INSTANT",
                   help="skip the network and render this instant across all "
                        "scales (see 'time travel' below)")
    p.add_argument("--epochs", action="store_true",
                   help="print a gallery of famous epochs and rollover cliffs")
    p.add_argument("--gpsd-host", default="127.0.0.1", metavar="HOST",
                   help="gpsd host for the satellite cross-check (default: 127.0.0.1)")
    p.add_argument("--gpsd-port", type=int, default=2947, metavar="PORT",
                   help="gpsd port (default: 2947)")
    p.add_argument("--no-gps", action="store_true",
                   help="skip the gpsd satellite cross-check")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--selftest", action="store_true",
                   help="run offline sanity checks (epoch math, round-trips) and exit")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def main(argv=None):
    global USE_COLOR
    args = build_parser().parse_args(argv)
    if args.no_color or args.json:
        USE_COLOR = False
    if args.server is None:
        args.server = list(DEFAULT_SERVERS)

    if args.selftest:
        return run_selftest()
    if args.epochs:
        run_epochs()
        return 0

    if args.at is not None:
        if args.watch is not None:
            print("--at is an offline destination; it can't be combined with "
                  "--watch", file=sys.stderr)
            return 1
        scales = scales_for(args.at)
        if args.json:
            print(json.dumps({"kind": "time-machine.instant", "scales": scales},
                             default=str))
        else:
            print(bold("── time-machine ─ destination reached " + "─" * 24))
            print(render_scales(scales))
        return 0

    if args.watch is not None:
        if args.watch <= 0:
            print("--watch interval must be positive", file=sys.stderr)
            return 1
        return run_watch(args)

    # One-shot report.
    results = poll_servers(args.server, args.timeout, args.samples)
    consensus = build_consensus(results, args.outlier_ms)

    if args.no_gps:
        gps_status, gps_data = "skipped", None
    else:
        gps_status, gps_data = gpsd_query(args.gpsd_host, args.gpsd_port,
                                          timeout=args.timeout + 1.0)
    if gps_data is not None and consensus is not None:
        gps_data["delta_vs_ntp_s"] = gps_data["offset"] - consensus["offset"]

    instant = time.time() + (consensus["offset"] if consensus else 0.0)
    scales = scales_for(instant)

    if args.json:
        print(json_report(results, consensus, gps_status, gps_data, scales))
    else:
        print(render_report(results, consensus, gps_status, gps_data, scales, args))

    if consensus is None:
        return 1
    if consensus["outliers"]:
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `... | head`); exit quietly.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
