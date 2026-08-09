# Memory

This fork exists because MiniMax-H3 does not fit in 48 GB the way upstream loads it. Everything
below is what keeps it fitting, in the order it matters.

## Measure it with the right instrument

Three tools report this process's memory and they disagree by two orders of magnitude. During a
live run that Activity Monitor showed at **29.13 GB**:

| Tool | Reported | Why |
|---|---|---|
| `ps -o rss=` | **0.13 GB** | MLX allocates through Metal; those buffers are not resident pages of the process |
| `vm_stat` "wired" | 12.3 GB | system-wide wired memory, not this process, and not all of the allocation |
| Activity Monitor | **29.13 GB** | counts unified-memory allocations — the real figure |
| `mx.get_active_memory()` | matches Activity Monitor | MLX's own accounting, available inside the process |

`memory.report()` prints MLX's numbers, and that is the measurement. `ps` is not a sanity check
here; it is simply blind. An earlier version of `docs/DESIGN.md` built a whole table on RSS and
concluded diffusion peaked at 10–11.5 GB. It does not — that was the fraction of the allocation
that happened to be file-backed resident pages.

## Phase residency

The four components are needed in disjoint phases. Loading all of them costs 45.9 GB of weights
before a single activation; loading each as its phase begins and dropping it as the phase ends
costs whatever the largest single phase costs.

| Phase | Held | Released at the end by |
|---|---|---|
| Text encoding, ~10 s | encoder **28.22 GB** | `LazyTextEncoder.encode` |
| Keyframe encoding | video VAE **5.21 GB** | `_release_vae_after` |
| Diffusion, hours | DiT 11.34 + LoRA 0.62 + table 0.13 = **12.09 GB** | `_decode_video` |
| Video decode | video VAE **5.21 GB** | `_decode_audio` |
| Audio decode | audio VAE **0.61 GB** | end of run |

Each unload is preceded by `mx.eval` on whatever the phase produced, and that ordering is the
whole mechanism. MLX is lazy: until the result is materialized it is a graph over the module's
parameters, and dropping the module frees nothing. `test_unload_without_eval_would_not_free`
measures this against the allocator, with a deliberately-lazy control to show the test has teeth.

`release()` then runs `gc.collect()` before `mx.clear_cache()`, in that order. Module trees form
reference cycles that refcounting does not break — measured: without the collect, the 28.2 GB
encoder survives into the diffusion loop.

Three of these unloads were added after the fact, and none of them announced its absence:

- the video VAE sat through the entire diffusion loop after encoding keyframes;
- the transformer sat through decoding, which is where the peak lands on a long clip;
- the video VAE was still resident when the audio VAE loaded.

Nothing raised. The run simply needed more memory than the machine had, which reads as slowness,
not as a bug.

## The allocator cache

MLX keeps freed buffers rather than returning them, because reuse beats reallocation. Unbounded,
that drifts upward across a long run: 29.13 GB observed against 12.09 of weights and roughly 9.3
of activations, so about **8 GB was cache the run had no use for**.

`memory.limit_cache()` caps it at 2 GB, applied when the pipeline is constructed. Override with
`H3_CACHE_LIMIT_GB`; zero disables the cache entirely, which is right on a machine already
swapping and wrong everywhere else.

The cost of not doing this is not an error, it is variance. In the run that exposed it, one step
took **818 s where its neighbours took 568** — the machine had 0.36 GB free and 3.98 GB swapped.
Freeing 1 GB elsewhere brought the next step back to 586 s.

## What else competes for the 48 GB

Worth checking before a multi-hour run, because the model's own budget assumes it has the machine:

```bash
ps -eo rss,command | awk '{s[$2]+=$1} END {for (a in s) if (s[a] > 500000) print s[a]/1048576" GB", a}'
```

In one measured case a browser held 4.05 GB across six processes and four editor sessions held
1.7 GB. Closing the editors alone moved free memory from 0.36 GB to 1.68 GB and swap from 3.98 to
3.56, and the step time recovered.

## What is not solved

**Activations scale with the packed sequence, not with anything convenient.** At 37,657 rows they
are roughly 9.3 GB, and the sequence grows with pixels *and* frames — so a 15 s clip at native
resolution is projected past 24 GB of activations on top of the 12.09 of weights. That is the
ceiling this fork does not lift; see `docs/RESULTS.md` for the wall-clock consequence.

**`iogpu.wired_limit_mb`** was raised to 44 GB during early measurement and resets on reboot.
Nothing that has actually run came near needing it, so reproducing anything here does not require
changing it. Methodology, not a prerequisite.
