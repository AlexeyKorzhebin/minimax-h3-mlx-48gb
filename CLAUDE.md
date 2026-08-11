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

# Writing tests in this repository

## A test is not written until you have watched it fail

Delete or invert the line the test protects, run it, see red, put the line back. Paste the
assertion error into whatever report the work produces.

Reading a test is not a substitute and has never once worked here. Over the week of 2026-08-04 to
08-11, seven review rounds found fourteen surviving mutations across seven tasks, every one of the
same shape — a test that pinned the *form* of an output and not its content. All fourteen read
fine. Rounds where the mutation check was demanded by hand closed in one pass; rounds where it was
only described in prose came back with another one.

Two traps specific to this codebase:

- **The fixture is eager, the real pipeline is lazy.** MLX does not compute until something forces
  it, and a test whose arrays are already materialized cannot see a bug that only exists while they
  are a graph. That is how a wrong `written_at` survived 293 tests and was found by running the CLI
  on a real job instead: `mx.save_safetensors` was executing the whole forward pass *inside* the
  write, eighteen minutes after the timestamp was taken.
- **An off-by-one in an ordering assertion.** "Some `mx.eval` happened just before the stamp" was
  satisfied by the *previous* iteration's eval. If the invariant is about who did something, assert
  the calling frame, not the sequence.

## A plan must be checked against its spec by someone who did not write the plan

Before the first implementer is dispatched. The spec-driven pipeline reproduces the plan author's
blind spots with high fidelity: on the CLI work, four of the surviving mutations came from code
samples in the plan itself, and the one real spec violation — a liveness threshold the spec
explicitly forbade — was written into the plan by the same person who wrote the prohibition and
then reviewed their own scan. `/codex` reading the plan and the spec side by side is enough; the
point is only that it is not the author.
