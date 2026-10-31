#!/usr/bin/env python3
"""Repeatable ldpctest runner: pins a core, records a manifest, saves raw logs
and writes JSON results with between-run statistics.

Example:
  ./ldpc_bench.py --build-dir ~/openairinterface5g/cmake_targets/ran_build/build \
      --cpu 2 --runs 21 --out out_cpu2 -- -l 8448 -n 2000 -s 3 -i 5

ldpctest prints each timer as: mean, max, std, count.
"""
import argparse, json, platform, re, statistics as st, subprocess, time
from pathlib import Path

TIMER = re.compile(r"^\s*(\S.*?):\s+([\d.]+) us;\s+([\d.]+) us;\s+([\d.]+) us;\s+(\d+);")
ERR = re.compile(r"^SNR ([-\d.]+), (BLER|BER|Uncoded BER) ([\d.]+) \((\d+)/(\d+)\)")
ITER = re.compile(r"^SNR ([-\d.]+), (Mean|Std|Max) iterations: ([\d.]+)")
SUMMARY_TIMERS = ("ldpc_decoder", "ldpc_encoder", "ldpc_encoder_optim")


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return r.stdout.strip()


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpufreq(cpu, name):
    return read(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/{name}")


def manifest(build_dir, cpu):
    return {
        "kernel": platform.release(),
        "os": sh("grep PRETTY_NAME /etc/os-release"),
        "cpu_model": sh("lscpu | grep 'Model name'"),
        "pinned_cpu": cpu,
        "p_cores": read("/sys/devices/cpu_core/cpus"),
        "e_cores": read("/sys/devices/cpu_atom/cpus"),
        "epp": cpufreq(cpu, "energy_performance_preference"),
        "governor": cpufreq(cpu, "scaling_governor"),
        "scaling_driver": cpufreq(cpu, "scaling_driver"),
        "scaling_max_khz": cpufreq(cpu, "scaling_max_freq"),
        "no_turbo": read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "power_profile": sh("powerprofilesctl get 2>/dev/null") or None,
        "mem_speed": sh("dmidecode -t memory 2>/dev/null | grep -i 'configured memory speed' | head -1") or None,
        "compiler": sh("gcc --version | head -1"),
        "build_type": sh("grep '^CMAKE_BUILD_TYPE' CMakeCache.txt", cwd=build_dir),
        "git_commit": sh("git rev-parse HEAD", cwd=build_dir),
        "git_dirty_files": sh("git status --porcelain | wc -l", cwd=build_dir),
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def parse_output(text):
    """Return (blocks, warnings), one block per SNR point.

    Timer sets are paired with SNR points by order; a new set starts when a
    timer name repeats.
    """
    timer_sets, cur, points = [], {}, {}
    for line in text.splitlines():
        m = TIMER.match(line)
        if m:
            name, mean, mx, sd, n = m.groups()
            if name in cur:
                timer_sets.append(cur)
                cur = {}
            cur[name] = {"mean_us": float(mean), "max_us": float(mx),
                         "std_us": float(sd), "count": int(n)}
            continue
        m = ERR.match(line)
        if m:
            snr, kind, val, bad, tot = m.groups()
            p = points.setdefault(snr, {"snr": float(snr)})
            key = kind.lower().replace(" ", "_")
            p[key] = float(val)
            if kind == "BLER":
                p["errors"], p["trials"] = int(bad), int(tot)
            continue
        m = ITER.match(line)
        if m:
            snr, kind, val = m.groups()
            points.setdefault(snr, {"snr": float(snr)})["iterations_" + kind.lower()] = float(val)
    if cur:
        timer_sets.append(cur)
    blocks = list(points.values())
    warnings = []
    if len(blocks) != len(timer_sets):
        warnings.append(f"{len(blocks)} SNR points but {len(timer_sets)} timer sets")
    for i, b in enumerate(blocks):
        b["timers"] = timer_sets[i] if i < len(timer_sets) else {}
    return blocks, warnings


def summarize(runs, discard):
    """Between-run statistics of the per-run means, using the first SNR point."""
    kept = [r for r in runs if not r["discarded"] and r["blocks"] and r["blocks"][0]["timers"]]
    out = {"runs_used": len(kept)}
    for name in SUMMARY_TIMERS:
        means = [r["blocks"][0]["timers"][name]["mean_us"]
                 for r in kept if name in r["blocks"][0]["timers"]]
        maxes = [r["blocks"][0]["timers"][name]["max_us"]
                 for r in kept if name in r["blocks"][0]["timers"]]
        if not means:
            continue
        out[name] = {
            "median_of_means_us": st.median(means),
            "cov_of_means": (st.stdev(means) / st.mean(means)) if len(means) > 1 else None,
            "min_mean_us": min(means), "max_mean_us": max(means),
            "worst_single_sample_us": max(maxes),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--cpu", type=int, required=True, help="CPU id to pin to (see lscpu --extended)")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--discard", type=int, default=1, help="leading process runs excluded from summary")
    ap.add_argument("--out", default="bench_out")
    ap.add_argument("--binary", default="ldpctest")
    ap.add_argument("ldpc_args", nargs=argparse.REMAINDER, help="after '--': arguments for ldpctest")
    a = ap.parse_args()
    args = a.ldpc_args[1:] if a.ldpc_args[:1] == ["--"] else a.ldpc_args

    out = Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    exe = str(Path(a.build_dir).resolve() / a.binary)
    cmd = ["taskset", "-c", str(a.cpu), exe] + args

    runs = []
    for i in range(a.runs):
        f0 = cpufreq(a.cpu, "scaling_cur_freq")
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=a.build_dir)
        wall = time.perf_counter() - t0
        blocks, warns = parse_output(p.stdout + "\n" + p.stderr)
        f1 = cpufreq(a.cpu, "scaling_cur_freq")
        (out / "raw" / f"run_{i:03d}.txt").write_text(p.stdout + p.stderr)
        runs.append({"run": i, "discarded": i < a.discard, "returncode": p.returncode,
                     "wall_s": wall, "freq_khz_before": f0, "freq_khz_after": f1,
                     "blocks": blocks, "parse_warnings": warns})
        print(f"run {i}: rc={p.returncode} snr_points={len(blocks)} "
              f"timer_sets={sum(1 for b in blocks if b['timers'])} wall={wall:.1f}s"
              + (f" WARN {warns}" if warns else ""))

    result = {"manifest": manifest(a.build_dir, a.cpu), "cmd": cmd,
              "summary": summarize(runs, a.discard), "runs": runs}
    (out / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
