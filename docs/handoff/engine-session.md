# Handoff: Gufo inference engine (session of 2026-10-28 to 2026-10-31)

Repo `/run/media/popov/d/DEV/repos/other/gufo`, branch `local` (fork of gufo-org/gufo `main` plus session
classes, API-key file, monitor and the work below). Host: Strix Halo gfx1151, 124 GB shared memory (the GPU pool IS
system RAM), Fedora, kernel 7.2.13. Production = Qwen3.8-Flash-Next UD-Q4_K_XL + shared Q8_0 MTP, behind
cloudflared (`ai-lm.d-popov.com` -> :1234, `ai-ui.d-popov.com` -> :8099).

## Rules learned the hard way
- **Never run two model-loading processes at once.** One server needs ~88-101 GB of the 122 GB pool. Two parallel
  `session_test` runs OOM-crashed and rebooted the host. Stop production first, run one GPU job at a time, check
  `free -g` before each, give every test server a fresh verified-free port and `try/finally` process-group cleanup.
  A leaked listener once invalidated a whole A/B (all arms hit the old server).
- A waiter using `pgrep -f <literal>` matches its own command line (use `[x]yz` form or a PID).
- Desktop apps share the pool: Brave (~9 GB) + VSCodium made the 3-slot sub class fail with `std::bad_alloc`.
- `/tmp` is tmpfs and was wiped by the reboot: helper scripts, `/tmp/fvenv`, `/tmp/devlab-assets` must be recreated.
- `d:` is a 150 GB disk that hit 100%; the 52 GB Qwen3.6-27B cache was moved to `/run/media/popov/DATA/llm/hub/`.
  Models live under `/run/media/popov/DATA/llm/hub`.

## State right now
- Serving: **pre-merge binary** `build/release/gufo.before-merge` (sha256 `ce8d58073ab90951`), started via
  `systemd-run --user --unit=gufo-flash-next --setenv=GUFO_BIN=.../gufo.before-merge /tmp/gufo-2sub.sh`, with the
  **sub class at 2 slots instead of 3** (`/tmp/gufo-2sub.sh` is a sed copy of the launcher; recreate if lost).
  Restore 3 slots: close Brave, then
  `systemd-run --user --unit=gufo-flash-next --collect --setenv=GUFO_BIN=<path to build/release/gufo.before-merge> ~/.local/bin/gufo-flash-next`.
- Launcher `~/.local/bin/gufo-flash-next` takes `GUFO_BIN` (default `build/release/gufo`; backup `.bak-before-gufo-bin`).
  The unit is transient: `systemctl restart` keeps the environment it was created with. Verify the running binary with
  `readlink /proc/<child pid>/exe` + `sha256sum`, never just `/ready`.
- `build/release/gufo` = **merged build** (sha256 `abd6291f6e131dc2`); backup `/run/media/popov/DATA/build/gufo.merged-20261030`
  (not mounted in the container, do not serve from it).
- Flags (production): `--speculative mtp`, `--prefill-chunk 1024 --decode-rounds 8`,
  `--session-class qwen3.8-flash-next=1x262144 --session-class qwen3.8-flash-next-sub=3x98304`, key file, mmproj.

## What was done in the engine
- Queue limits (commit `ed2772cc`): per-client pending default now `min(--max-pending, 8)` (was = max-pending=16);
  `--max-connections` 16 (503 overloaded). Documented in `docs/SERVER.md`. Verified on the real server: 14
  simultaneous requests from one client -> 9x200, 5x429. The live binary predates this; only the merged build has it.
- `tools/bench/prefill-decode-sweep.py`: sweeps `--prefill-chunk` x `--decode-rounds` with one decoding stream beside a
  cold ~37.6K prefill, in one multi-slot class (refuses 1-slot classes; needs `--model qwen3.8-flash-next-sub`).
  Results table (2026-10-30) is in `docs/SERVER.md`. 1024/8 stays; 2048/16 is ~7% faster prefill and +6% decode
  but pauses up to 1.41 s vs 0.82 s.
- Upstream `origin/main` merged (merge commit `76339300`, up to #882 `5d66de8c`). **`192b75a8` (#875) is NOT merged.**
  Conflicts were the trailing-parameter chain of `session_class` and `cache_persist` (order now `model, cache_persist`,
  `session_class, persist`). New upstream features now present: `cache_persist=false`, logprobs, ffmpeg/libav dependency.
- `tests/cli/serve_test.py` help-text assertion updated for the new per-client wording.
- `session_test.cpp` fix (`164c19c8`): the "restored draft table left the small head" check was seed-dependent; #882
  (confidence-gated MTP chains) changes sampled trajectories (4/10 seeds leave the small head with #882, 1/10 without).
  Now requires a sampled restore to use the small head at least once.

## Verification of the merged build (what is and is not proven)
Passed: Devlab Flash-Next groups 24/25 suites (`cache-stalls` fails identically on the pre-merge binary: pre-existing);
prompt_lookup 47/47 with 5264/5864 drafts accepted and MTP loaded; 20/20 Flash-Next GPU operator tests; greedy output
hashes identical to pre-merge; three full `session_test` runs passed after the test fix.
Open:
1. Devlab paired timing flagged **slower prefill** on the merged build (short 1295 -> 2168 ms, long 7713 -> 9412 ms,
   resume 315 -> 534 ms; decode ~unchanged). My follow-up A/B was invalid (leaked listener), so this is unconfirmed.
2. Earlier intermittent `session_test` failures (lookup batch equivalence, MTP cache replay `restore=1`, the latter also
   once with #882 reverted) were not diagnosed; they did not recur in the last three runs.
3. Upstream's +11% MTP decode claim was not reproduced.
Decision pending: serve merged or stay on pre-merge. Recommendation was to stay until one clean timing A/B (fresh ports,
both binaries, alternating, production stopped) confirms prefill is not slower.
Environment failures reported separately (not engine bugs): 7 skipped CPU tests (missing models), 6 CPU failures
(4 need numpy, 2 need ffmpeg encoders in the build image).

## How to build / test
- Build image `localhost/gufo-build` (rebuilt with libav dev packages; previous tagged `before-ffmpeg`). Run builds
  in the container: mount repo, `$HOME/rocm/install`, `$B/tmp`; env `ROCM_PATH/HIP_PATH/LD_LIBRARY_PATH` as in the
  launcher; podman root `/run/media/popov/DATA/build/podman/storage`, runroot `/run/user/1000/podman-build`.
  Release: `cmake --build build/release --target gufo`. The host can run the built binary with
  `LD_LIBRARY_PATH=$HOME/rocm/install/lib:...rocm_sysdeps/lib:...llvm/lib`.
- GPU tests: `build/gpu-test` needed `-DCMAKE_PREFIX_PATH=$HOME/rocm/install -DCMAKE_HIP_COMPILER=.../llvm/bin/clang++
  -DHIPCUB_INCLUDE_DIR/ROCPRIM_INCLUDE_DIR/ROCWMMA_INCLUDE_DIR=$HOME/rocm/install/include
  -DCMAKE_EXE_LINKER_FLAGS=-no-pie`. Targets: `qwen38_flash_next_tests`, `qwen38_flash_next_session_test`.
  `session_test --model <first shard> --mtp-model <MTP gguf>` (full run ~3.5 min, loads the model ~4 times).
- Functional/Devlab: needs a venv from `tests/functional/requirements.txt`; `devlab_plan.py --repo . --base A --head B
  --tier pr --benchmark --output plan.json`, then `devlab_run.py` with an assets JSON mapping `flash-next` to a dir that
  contains symlinks `UD-Q4_K_XL`, `MTP`, `mmproj-BF16.gguf`. `tests/functional/prompt_lookup.py` for draft use.
- Format: `clang-format` (v23 here; CI check is `tools/ci/check-format.py`). AGENTS.md rules apply (Conventional Commits).

## Suggested next steps
1. Restore 3 sub slots (close Brave) and confirm `load_completed` in `~/.local/state/gufo/gufo-flash-next.log`.
2. One clean timing A/B of merged vs pre-merge (prefill at 1.5K/6K/12K, MTP decode on 5 prompts x 384 tokens);
   then decide the binary. Diagnose the intermittent `session_test` failures if the merged binary is chosen.
3. Consider merging #875 afterwards; re-run Devlab for the merged range.
4. Kernel 7.2.13 vs 7.2.12 (previous boot) is a suspect if memory headroom stays lower than before the reboot.
