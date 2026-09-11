"""
Put a deliberately unreliable link between MavGCS and a real aircraft.

Some faults only exist on a radio that drops packets, and a good radio
will not oblige. The parameter retry is one: on a clean link everything
arrives first time and the retry never runs, so connecting to a healthy
aircraft proves the happy path and nothing else.

This sits in the middle and throws a given fraction of packets away, in
both directions, so the failure can be produced at will with the real
program talking to the real vehicle.

    python tools/lossy_link.py --listen 14599 --forward 127.0.0.1:14560 --drop 40

Wiring it in, with MAVProxy as the hub it already is:

    mavproxy.exe --master=udpin:0.0.0.0:14550 --out=udp:127.0.0.1:14599
    python tools/lossy_link.py --listen 14599 --forward 127.0.0.1:14560 --drop 40

then point MavGCS at UDP (listen) on 14560 as usual. Everything the
aircraft sends now reaches MavGCS through the dropper, and everything
MavGCS sends goes back the same way.

Without MAVProxy, --listen can take the aircraft's stream directly if the
bridge is told to send there instead.

What to look for at --drop 40, which is far worse than any real link:

  * Settings should still fill in the elevator line, taking longer.
  * The message log should show it giving up and saying so if the drop
    rate is high enough that it never completes - try --drop 85.
  * Nothing should sit silently on "not detected" with no explanation,
    which is what it did before the retry existed.

Drops are symmetric and independent per packet. A real radio loses in
bursts rather than uniformly, so this is a harsher test in one way and a
gentler one in another - it will not reproduce a burst that takes out a
whole reply sequence.
"""

import argparse
import random
import select
import socket
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--listen", type=int, required=True,
                    help="UDP port to receive the aircraft's stream on")
    ap.add_argument("--forward", required=True, metavar="HOST:PORT",
                    help="where MavGCS is listening, e.g. 127.0.0.1:14560")
    ap.add_argument("--drop", type=float, default=30.0,
                    help="percent of packets to throw away (default 30)")
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the random seed, to repeat a run exactly")
    args = ap.parse_args()

    if not 0 <= args.drop < 100:
        sys.exit("--drop must be between 0 and 100")
    if args.seed is not None:
        random.seed(args.seed)

    host, _, port = args.forward.rpartition(":")
    forward = (host, int(port))

    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.bind(("0.0.0.0", args.listen))
    down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    source = None                   # learned from the first packet in
    kept = {"down": 0, "up": 0}
    lost = {"down": 0, "up": 0}
    fraction = args.drop / 100.0
    reported = time.monotonic()

    print("Dropping %.0f%% of packets in both directions." % args.drop)
    print("  aircraft -> :%d -> %s:%d -> MavGCS"
          % (args.listen, forward[0], forward[1]))
    print("Ctrl-C to stop.\n")

    try:
        while True:
            ready, _, _ = select.select([up, down], [], [], 1.0)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is up:
                    source = addr
                    way, out, to = "down", down, forward
                else:
                    if source is None:
                        continue        # nothing has come the other way yet
                    way, out, to = "up", up, source
                if random.random() < fraction:
                    lost[way] += 1
                    continue
                kept[way] += 1
                try:
                    out.sendto(data, to)
                except OSError:
                    pass

            now = time.monotonic()
            if now - reported >= 5.0:
                reported = now
                for way in ("down", "up"):
                    total = kept[way] + lost[way]
                    if total:
                        print("  %-4s kept %5d  dropped %5d  (%.0f%%)"
                              % (way, kept[way], lost[way],
                                 100.0 * lost[way] / total))
                print("")
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
