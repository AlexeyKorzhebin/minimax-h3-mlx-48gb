# Running experiments on this machine

## Never start a run that is not wrapped in `caffeinate`

```bash
caffeinate -dimsu <the run, or the queue script that drives it>
```

Not the generate command inside the script — the whole script, so the assertion outlives every
run in the queue and the gaps between them.

Nothing in this fork takes a sleep assertion, and a run does not look like activity to macOS: it
pins the GPU at full power while the keyboard, the display and the network sit idle, which is the
exact profile `powerd` reads as an idle machine. On 2026-08-10 that cost a 1344x768 run at step 2
of 7 — the Mac idle-slept four times during it and kernel-panicked coming out of the last one,
`agx_power(6) failed to transition to state 0`, forty-five seconds after entering Maintenance
Sleep. `docs/MEMORY.md` has the timeline.

Check `pmset -g` before a long run. `sleep 1` and `displaysleep 30` are the defaults and both are
fatal here.

## Logs go on disk, never in `/tmp`

`~/Research/TestVideo/_логи/` for run logs, `~/Research/TestVideo/_очередь/` for the queue
scripts that produce them. The same reboot that killed the run wiped `/tmp`, which is where every
log from that night lived — the crash and the loss of the evidence about the crash were one event.

## Outputs

`H3_OUTDIR` is `~/Research/TestVideo`, deliberately outside `~/models` so the videos can be
deleted without touching the weights. It must never default to anywhere inside `~/models`.
