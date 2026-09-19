"""The flight summary, and the efficiency it now reports.

Every other figure in that window describes the flight. Efficiency
describes the aircraft: milliamp-hours per kilometre is what compares
one propeller, one airframe or one loading against another across
flights of different lengths, which is the only reason it is worth
computing rather than leaving to arithmetic on the other two rows.

A ratio is only as good as its denominator, so most of what is checked
here is when it declines to answer. A stationary aircraft still
accumulates GPS scatter as distance flown, and dividing a real current
draw by a metre of noise gives a confident four-figure number that means
nothing at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as app_main

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


def stats(distance_m=6350.0, consumed=541.0, have_sensor=True):
    s = app_main.FlightStats()
    s.distance_m = distance_m
    if have_sensor:
        s.mah_start, s.mah_end = 0.0, consumed
    return s


def rows_of(s):
    return dict(s.rows())


print("")
print("1. the number itself")

# The flight in the report this was asked for: 6.35 km, 541 mAh.
r = rows_of(stats())
note("a real flight divides out correctly", r["Efficiency"] == "85.2 mAh/km",
     "%s, and 541/6.35 is %.1f" % (r["Efficiency"], 541 / 6.35))
note("the rows it is derived from are unchanged",
     r["Distance flown"] == "6.35 km" and r["Consumed"] == "541 mAh",
     "%s, %s" % (r["Distance flown"], r["Consumed"]))

# A glider-like figure and a thirsty one, to show the format holds at
# both ends rather than only near 85.
note("an efficient aircraft reads low",
     rows_of(stats(20000.0, 180.0))["Efficiency"] == "9.0 mAh/km",
     rows_of(stats(20000.0, 180.0))["Efficiency"])
note("and a thirsty one reads high",
     rows_of(stats(1200.0, 900.0))["Efficiency"] == "750.0 mAh/km",
     rows_of(stats(1200.0, 900.0))["Efficiency"])


print("")
print("2. where it sits")

order = [label for label, _ in stats().rows()]
note("it is directly under Consumed, where it was asked for",
     order.index("Efficiency") == order.index("Consumed") + 1,
     " / ".join(order[-3:]))
note("and it is the last row", order[-1] == "Efficiency")

# The Copy button and the saved report both go through as_text, so a row
# that only exists in the dialog would be missing from what gets pasted
# into a log.
text = stats().as_text()
note("it reaches the copied text too", "Efficiency" in text
     and "85.2 mAh/km" in text,
     [l for l in text.splitlines() if "Efficiency" in l][:1])
# Every value starts in the same column, which is what as_text's ljust
# is for. Checked against the contract rather than by splitting on runs
# of spaces - a label shorter than the padding makes that ambiguous, and
# the first version of this check failed on correct output.
pairs = stats().rows()
width = max(len(label) for label, _ in pairs)
lines = text.splitlines()
note("and every value starts in the same column there",
     len(lines) == len(pairs)
     and all(line.startswith(label.ljust(width) + "  ")
             for (label, _), line in zip(pairs, lines)),
     "value column at %d, last line %r" % (width + 2, lines[-1]))


print("")
print("3. when it refuses to answer")

# The bench case: armed, drawing current, never went anywhere. GPS
# scatter alone can put tens of metres on the odometer.
note("a flight that went nowhere reports nothing",
     rows_of(stats(12.0, 60.0))["Efficiency"] == "--",
     rows_of(stats(12.0, 60.0))["Efficiency"])
note("nor does it divide by a distance of zero",
     rows_of(stats(0.0, 60.0))["Efficiency"] == "--")
# Just under and just over the line, so the threshold is the thing being
# tested rather than the general shape.
limit = app_main.FlightStats.MIN_EFFICIENCY_DISTANCE_M
note("just under the threshold declines",
     rows_of(stats(limit - 1.0, 50.0))["Efficiency"] == "--",
     "%.0f m" % (limit - 1.0))
note("and just over it answers",
     rows_of(stats(limit + 1.0, 50.0))["Efficiency"] != "--",
     "%.0f m gives %s" % (limit + 1.0,
                          rows_of(stats(limit + 1.0, 50.0))["Efficiency"]))

# The autopilot's consumption counter reset mid-flight. The subtraction
# is then measuring the reset, not the aircraft.
note("a counter that went backwards is not reported as efficiency",
     rows_of(stats(5000.0, -120.0))["Efficiency"] == "--",
     rows_of(stats(5000.0, -120.0))["Efficiency"])

# No current sensor at all: there is no Consumed row, so there must be
# no Efficiency row either rather than an orphan reading "--".
no_sensor = rows_of(stats(have_sensor=False))
note("without a current sensor neither row appears",
     "Consumed" not in no_sensor and "Efficiency" not in no_sensor,
     ", ".join(k for k in ("Consumed", "Efficiency") if k in no_sensor)
     or "neither")


print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
