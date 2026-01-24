#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
iq_recorder_no_gui.py

Headless GNU Radio / UHD IQ recorder with deterministic, PPS-aligned start timing.

Version 2 adds:
- Mandatory verification of external 10 MHz reference lock (B200 ref_locked sensor)
- Deterministic PPS-based device time initialization
- Robust timed start scheduled on an integer device second guaranteed to be
  safely in the future (avoids startup overruns)
- Elimination of placeholder output files (no stray armed.ci8 artifacts)
- CI8 recording path suitable for long-duration, phase-coherent captures

This script is intended for precision RF observation where absolute timing,
frequency coherence, and repeatability matter more than UI convenience.

Author: Scott Tilley

MIT License

Copyright (c) 2026 Scott Tilley

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from gnuradio import gr, blocks, uhd
import time
import datetime
import signal
import sys


def assert_external_ref_locked(usrp, mboard=0, settle_s=0.25):
    """
    Hard gate: ensure external 10 MHz reference is actually locked before recording.
    Prints an error and exits(1) if not locked or sensor not available.
    """
    time.sleep(settle_s)

    try:
        names = usrp.get_mboard_sensor_names(mboard)
    except Exception as e:
        print(f"[ERROR] Unable to read mboard sensor names: {e}")
        sys.exit(1)

    if "ref_locked" not in names:
        print(f"[ERROR] 'ref_locked' sensor not available. Sensors: {names}")
        sys.exit(1)

    try:
        locked = usrp.get_mboard_sensor("ref_locked", mboard).to_bool()
    except Exception as e:
        print(f"[ERROR] Unable to read 'ref_locked' sensor: {e}")
        sys.exit(1)

    if not locked:
        print("[ERROR] External 10 MHz reference NOT locked")
        sys.exit(1)

    print("[INFO] External 10 MHz reference locked")


class iq_recorder(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "IQ Recorder (Headless)", catch_exceptions=True)

        ############################
        # Parameters
        ############################
        self.samp_rate = 1e6
        self.center_freq = 2211e6
        self.gain = 76

        # IMPORTANT: don't create a junk placeholder file
        self.iq_out = "/dev/null"

        ############################
        # USRP Source
        ############################
        self.usrp = uhd.usrp_source(
            ",".join(("", "")),
            uhd.stream_args(
                cpu_format="fc32",
                channels=[0],
            ),
        )

        # External ref/PPS
        self.usrp.set_clock_source("external")
        self.usrp.set_time_source("external")

        # RF / stream params
        self.usrp.set_samp_rate(self.samp_rate)
        self.usrp.set_center_freq(self.center_freq, 0)
        self.usrp.set_gain(self.gain, 0)
        self.usrp.set_antenna("RX2", 0)
        self.usrp.set_bandwidth(self.samp_rate, 0)

        # PPS discipline (keeps UHD happy if PPS shows up "unknown" at init)
        self.usrp.set_time_unknown_pps(uhd.time_spec(0.0))

        ############################
        # CI8 recording path
        ############################
        self.mul = blocks.multiply_const_cc(127.0)
        self.c2ic = blocks.complex_to_interleaved_char(False, 1.0)

        self.file_sink = blocks.file_sink(
            gr.sizeof_char,
            self.iq_out,
            False
        )
        self.file_sink.set_unbuffered(False)

        ############################
        # Connections
        ############################
        self.connect(self.usrp, self.mul)
        self.connect(self.mul, self.c2ic)
        self.connect(self.c2ic, self.file_sink)

    def set_iq_out(self, fname):
        self.iq_out = fname
        self.file_sink.open(fname)


def main():
    tb = iq_recorder()
    usrp = tb.usrp

    # ---- HARD GATE: external 10 MHz must be locked ----
    assert_external_ref_locked(usrp)

    ############################
    # PPS-aligned timed start
    ############################

    # Define device time = 0 exactly at the next PPS edge
    usrp.set_time_next_pps(uhd.time_spec(0.0))

    # Ensure PPS edge has passed and device time is running
    time.sleep(1.2)

    # Pick a start time that is ALWAYS >= ~2s in the future, and on an integer device second
    now_dev = usrp.get_time_now().get_real_secs()
    start_dev_time = int(now_dev) + 3
    if (start_dev_time - now_dev) < 2.0:
        start_dev_time = int(now_dev) + 4

    # Filename corresponds to scheduled start (best-effort UTC alignment)
    now_utc = datetime.datetime.utcnow().replace(microsecond=0)
    delta_s = start_dev_time - now_dev
    start_utc = (now_utc + datetime.timedelta(seconds=delta_s)).replace(microsecond=0)

    fname = start_utc.isoformat().replace(":", "-") + ".ci8"
    tb.set_iq_out(fname)

    # Arm timed start at exact device time
    usrp.set_start_time(uhd.time_spec(start_dev_time))

    ############################
    # Run
    ############################
    tb.start()

    def shutdown(sig=None, frame=None):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Block forever
    signal.pause()


if __name__ == "__main__":
    main()
