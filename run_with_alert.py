"""
run_with_alert.py -- run a job script and alert the ops webhook if it fails.

Usage: python run_with_alert.py gex_vex_unified_daily.py
Output is streamed through unchanged so Railway logs look the same. Exits
with the job's own exit code, so Railway still marks the run failed.
"""

import subprocess
import sys
from collections import deque

from ops_alert import post_ops_alert


def main():
    script = sys.argv[1]
    tail = deque(maxlen=12)
    proc = subprocess.Popen([sys.executable, "-u", script], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(line, end="")
        tail.append(line.rstrip())
    code = proc.wait()
    if code != 0:
        post_ops_alert(f"\U0001F6A8 **{script} FAILED** (exit {code})\n```\n" + "\n".join(tail)[-1500:] + "\n```")
    sys.exit(code)


if __name__ == "__main__":
    main()
