# time-machine

A clock-drift auditor in one Python file, stdlib only. It measures how far
your system clock sits from multiple independent time authorities and renders
one instant across every major time scale.

An honest note on framing: there is no single live master clock. UTC is a
post-hoc committee construction — the BIPM averages a few hundred atomic
clocks and publishes the result (Circular T) weeks after the fact. What you
can reach over a network are local *realizations* of UTC, each steered toward
that paper average. This tool audits the negotiation, not an oracle.

## Quick start

```console
$ python3 timemachine.py                 # one-shot audit against 4 authorities
$ python3 timemachine.py --json          # machine-readable, for piping
$ python3 timemachine.py --watch 10      # re-poll every 10 s, fit drift in ppm
$ python3 timemachine.py --selftest      # offline sanity checks
$ python3 timemachine.py --epochs        # gallery of famous zero-points
$ python3 timemachine.py --at bttf       # offline time travel across all scales
$ python3 timemachine.py --at 2015-10-21T16:29Z
$ python3 timemachine.py --at -- -2d     # relative jumps: ±N[smhdwy]
```

## What it does

- **Hand-built SNTP client** over UDP/123 (48-byte packet, LI=0/VN=4/Mode=3).
  Computes offset `((T2−T1)+(T3−T4))/2` and delay `(T4−T1)−(T3−T2)` from the
  four timestamps, and verifies the origin-timestamp echo against spoofed
  replies. Servers are queried in parallel; unreachable hosts degrade
  gracefully.
- **Cross-source disagreement**: picks the best source by lowest round-trip
  delay, reports the max pairwise offset spread, and flags any server whose
  offset deviates from the median by more than `--outlier-ms` (default 100 ms)
  as a possibly lying or misconfigured source. Exits nonzero (2) when an
  outlier is detected, so it's scriptable and monitorable.
- **GPS cross-check via gpsd** (optional): if gpsd is on `localhost:2947`, it
  speaks the JSON protocol over a plain TCP socket (no `gps` package), reads
  TPV messages, and compares satellite-derived time against the NTP consensus.
  No gpsd → one line noting the cross-check is unavailable, and it continues.
- **Time scales** for the consensus instant: UTC, local, TAI (UTC+37),
  GPS week + time-of-week (UTC+18, plus legacy mod-1024 week), Unix seconds
  and nanoseconds, NTP timestamp (1900 epoch, decimal and 32.32 hex),
  Julian Date, Modified JD, Greenwich Mean Sidereal Time, Swatch .beats,
  day-of-year, and ISO week date. Leap-second constants live at the top of
  the file with a note that they only change on IERS Bulletin C and bump
  together (TAI−GPS is pinned at 19 s).
- **Watch mode** (`--watch N`): tracks min/max/mean offset and estimates the
  drift rate in ppm by linear regression of offset against `time.monotonic()`
  — so a stepped system clock can't poison the fit. Ctrl-C prints the session
  verdict. With `--json` it emits NDJSON, one report per poll.

## Options

```
--server HOST[:PORT]   NTP server, repeatable (default: time.nist.gov,
                       time.cloudflare.com, time.google.com, pool.ntp.org)
--timeout SEC          per-query UDP timeout (default 2)
--samples N            exchanges per server, lowest-delay kept (default 1)
--outlier-ms MS        outlier threshold vs median offset (default 100)
--watch N / --json / --at INSTANT / --epochs / --selftest
--gpsd-host / --gpsd-port / --no-gps / --no-color
```

## Exit codes

| code | meaning |
|------|---------|
| 0    | audit completed, sources agree |
| 1    | no usable time source / self-test failure |
| 2    | at least one source flagged as an outlier |

## License

MIT — see [LICENSE](LICENSE).
