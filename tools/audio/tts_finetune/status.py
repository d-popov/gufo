#!/usr/bin/env python3
"""Watch, stop and inspect a train.py run from another console. Needs only Python 3.

    status.py OUTPUT_DIR            live view, refreshed every 2 s (Ctrl-C leaves the view; the run goes on)
    status.py OUTPUT_DIR --once     print the status once
    status.py OUTPUT_DIR stop       ask the run to save and stop after its current step
    status.py OUTPUT_DIR history    the loss curve, one row per progress line
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def read_state(out: Path) -> dict | None:
    try:
        return json.loads((out / "state.json").read_text())
    except (OSError, ValueError):
        return None


def read_progress(out: Path, last: int = 0) -> list[dict]:
    try:
        lines = (out / "progress.jsonl").read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-last or None:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass  # a line being written
    return rows


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def clock(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "-"
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def ago(then: float | None) -> str:
    return "never" if not then else f"{clock(time.time() - then)} ago"


def describe(out: Path) -> list[str]:
    state = read_state(out)
    if state is None:
        return [f"{out}: no state.json yet (the run has not started, or this is not its output directory)"]
    status = state.get("status", "?")
    running = alive(state.get("pid"))
    if status in ("running", "saving", "starting") and not running:
        status = f"{status}, but the process is gone: it crashed or was killed; resume with --resume auto"
    steps, total = state.get("global_step", 0), state.get("total_steps") or 0
    per_epoch = state.get("steps_per_epoch") or 0
    recent = [r for r in read_progress(out, 30) if r.get("seconds_per_step")]
    per_step = sum(r["seconds_per_step"] for r in recent) / len(recent) if recent else None
    losses = [r["loss"] for r in recent]
    lines = [f"{status.upper()}  pid {state.get('pid')}  {out}"]
    if total:
        lines.append(f"epoch {state.get('epoch', 0) + (1 if status == 'done' else 0)}/{state.get('epochs')}  "
                     f"step {state.get('step_in_epoch', 0)}/{per_epoch}  total {steps}/{total} ({100 * steps / total:.1f}%)")
    if recent:
        lines.append(f"loss {losses[-1]:.4f}  (mean of the last {len(losses)} lines {sum(losses) / len(losses):.4f})  "
                     f"lr {recent[-1].get('lr', 0):.2e}")
        eta = (total - steps) * per_step if per_step and total and running else None
        lines.append(f"{per_step:.1f} s/step  {recent[-1].get('samples_per_s', 0):.2f} samples/s  "
                     f"trained {clock(state.get('train_seconds'))}  remaining {clock(eta)}")
    lines.append(f"checkpoint {state.get('last_checkpoint', 'none yet')} ({ago(state.get('last_checkpoint_at'))})  "
                 f"samples seen {state.get('samples_seen', 0)}")
    if (out / "STOP").exists():
        lines.append("a stop was requested; it saves after the current step")
    if status.startswith("stopped"):
        lines.append("resume:  train.py ... --resume auto")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("command", nargs="?", choices=["stop", "history"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    out = args.output_dir

    if args.command == "stop":
        state = read_state(out)
        if state is None or not alive(state.get("pid")):
            print("the run is not running")
            return 1
        (out / "STOP").write_text("stop\n")
        print(f"stop requested for pid {state['pid']}; it saves a checkpoint after the current step, then exits")
        return 0
    if args.command == "history":
        for r in read_progress(out):
            print(f"{clock(r['t'] - read_progress(out, 0)[0]['t'])}  epoch {r['epoch']} step {r['step']:>5}/{r['steps_per_epoch']}  "
                  f"global {r['global_step']:>6}  loss {r['loss']:.4f}  {r['seconds_per_step']:.1f} s/step")
        return 0
    if args.once:
        print("\n".join(describe(out)))
        return 0
    try:
        while True:
            sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(describe(out)) + "\n\n(Ctrl-C leaves this view; the run continues)\n")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
