# ldpctest benchmark harness (prototype)

A small runner that turns `ldpctest`, the LDPC unitary simulator in OpenAirInterface, into repeatable measurements: it pins a core, records a system manifest, keeps raw logs, writes JSON results, and reports noise between separate process runs.

This is a personal prototype written while exploring the project. It is not upstream code. It is a benchmarking harness around `ldpctest`, not a test suite: it times the encoder and decoder, and does not check decoding correctness itself.

## Run

```
cd ~/openairinterface5g/cmake_targets
./build_oai --phy_simulators --ninja

python3 ldpc_bench.py \
  --build-dir ~/openairinterface5g/cmake_targets/ran_build/build \
  --cpu 2 --runs 21 --out out_cpu2 -- -l 8448 -n 2000 -s 3 -i 5
```

Find core ids with `lscpu --extended`. Output: `out_cpu2/raw/` (one log per run) and `out_cpu2/result.json` (manifest, every run, summary).

`dmidecode` needs root. Without it the `mem_speed` field in the manifest is `null`, which means "not recorded", not "unknown hardware".

## Method

- One process run = one `ldpctest` execution with `-n 2000` trials, pinned with `taskset`.
- About 20 process runs per configuration (see `runs_used` in each `result.json`); the first is discarded.
- Summary uses the first SNR point (3.0 dB): median of the per-run means, the coefficient of variation (CoV) of those means across runs, and the worst single sample seen.
- `ldpctest` prints each timer as mean, max, std, count (confirmed against `ldpctest.c`).
- The manifest records kernel, OS, CPU, governor, EPP, turbo setting, compiler, build type, git commit and the pinned core's frequency before and after each run.

## Results

Machine: Intel Core Ultra 5 laptop, 16 GB RAM, Fedora Linux 44 Workstation (kernel 7.2.11-200.fc44.x86_64), OpenAirInterface commit `cfee4c2`, governor and EPP `performance`, turbo enabled. Workload: one 8448-bit segment, BG1, rate 1/3, SNR 3.0 dB, up to 5 decoder iterations.

| Core type          | Clock (snapshot) | Decoder, median of means | Run-to-run CoV | Worst single decode | SIMD encoder |
|--------------------|------------------|--------------------------|----------------|---------------------|--------------|
| P-core (cpu 2)     | ~4.9 GHz         | 96 µs                    | 1.6%           | 570 µs              | 13.8 µs      |
| E-core (cpu 6)     | ~4.0-4.3 GHz     | 128 µs                   | 2.5%           | 1380 µs             | 20.7 µs      |
| LP E-core (cpu 12) | ~2.5 GHz         | 327 µs                   | 4.4%           | 1945 µs             | 57.8 µs      |

A 30 kHz slot is 500 µs.

## Observations

- Decode time on the E-core is 1.33x the P-core and on the LP E-core 3.4x. Clock speed explains roughly 1.2x and 2x of that; the remainder is per-core behaviour that has not been investigated.
- Run means repeat within about 2-5% across separate processes. Differences smaller than that are not distinguishable from noise on this setup.
- The worst single decode exceeded 500 µs on every core type. These are single samples on an untuned laptop and are most likely OS interference; this was not investigated.

## Bug found while testing: `ldpctest -S N`

With `-S N` for N > 1, `ldpctest` reported exactly N-1 segments in error at every SNR (BER close to 0.5*(N-1)/N), so BLER was 1 at all SNRs. `-S 1` passed, and both library versions behaved the same.

The cause was two inconsistent buffer offsets in `openair1/PHY/CODING/TESTBENCH/ldpctest.c` (non-CUDA path only):

- The decoder input for segment `j` was read at `channel_output_fixed[j]`, but the noisy samples are written at `j * (384 * 68)`.
- The decoder output for segment `j` was written at `j * Kprime`, while the comparison reads at `j * (Kprime >> 3)`.

Every segment after the first was therefore decoded from, or compared against, the wrong region. An earlier attempt that changed only the compare stride made no difference, which fits with two separate offsets being wrong.

A two-line fix is proposed upstream in [PR #960](https://github.com/duranta-project/openairinterface5g/pull/960) (under review). The timing results above use `-S 1` (one segment) and are not affected by this bug.

## Not yet verified

- Which `libldpc` build is the SIMD one. The encoder timers suggest `libldpc.so` is much faster than `libldpc_orig.so` (about 31x on the P-core), but I have not checked the source.
- At small `-n`, the printed max can be lower than the mean. Possible cause: the cold first call is counted in mean/std but not in max. Not yet checked in `common/utils/time_meas.*`.
- Whether `ldpctest` uses a fixed noise seed. If it does not, iteration counts can differ between runs, and some of the run-to-run spread may come from the decoder rather than the hardware.
- What causes the extra slowdown on the LP E-core beyond its lower clock.

## Limitations

- One machine, one workload, one SNR point; means rather than worst case.
- No core isolation (`isolcpus`, `nohz_full`) or IRQ pinning.
- Core types are measured one after another, not interleaved, so thermal drift or background load could differ between them.
- Core frequency is read before and after each run, not during it, so it cannot show throttling inside a run.
- BLER and iteration counts are parsed but not checked or reported in the summary, so a run that decodes differently would not be flagged.
- No power-profile name was recorded (governor and EPP were `performance`; see the manifests).

## Files

- `ldpc_bench.py`: runner and parser
- `out_cpu2/`, `out_cpu6/`, `out_cpu12/`: raw logs and `result.json` per core type
