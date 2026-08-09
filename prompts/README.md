# Test prompts

One prompt per file, used to compare runs across step counts, LoRA strengths and canvases.
Kept in the repo rather than in a scratch directory because a comparison is only meaningful if
the prompt is byte-identical between runs — and a prompt that lives in `/tmp` does not survive
the week. One of these was lost mid-experiment exactly that way.

```bash
h3 generate "$(cat prompts/centaur-battle.txt)" \
  --width 896 --height 576 --duration 10 --steps 8 \
  --turbo-lora ~/models/turbo/minimax_h3_turbo_v4_step600_ema.safetensors
```

| file | what it exercises | notes |
|---|---|---|
| `dragon-flight.txt` | smooth motion, one subject | the reference clip for step-count and LoRA-strength sweeps |
| `centaur-battle.txt` | multi-shot with explicit timing, two figures, fast motion | the hardest case here; matches a clip generated on the unquantized model, so it doubles as a quantization check |
| `galloping-horse.txt` | fast rhythmic motion | the case the LoRA's author warns smears at low step counts |
| `tango-dancers.txt` | two interlocking figures, faces | shows why naming the shot matters — see `docs/RESULTS.md` |
| `tango-dancers-wide.txt` | the same scene, framed wide | the failing version, kept as the contrast |
| `fisherman-portrait.txt` | near-static, close-up face | checks the LoRA does not add motion where none belongs |
| `lighthouse-storm.txt` | long-form, weather and water | used for the 10 s and 15 s duration runs |
