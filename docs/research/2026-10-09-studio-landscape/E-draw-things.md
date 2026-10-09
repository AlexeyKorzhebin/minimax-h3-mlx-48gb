# Draw Things как исполнитель (проверено на Маке 09.10.2026)

**На Маке уже скачаны** (`draw-things-cli models list --downloaded-only`):
- `krea_2_turbo_q8p.ckpt`
- `z_image_turbo_1.0_q8p.ckpt`
- `z_image_1.0_q8p.ckpt`

**Каталог** (`models list`):
- есть: LTX-2 и 2.3 (22B dev/distilled), Wan 2.2 A14B (t2v, i2v), LongCat-Video-Avatar;
- **нет MiniMax H3.**

**`draw-things-cli generate`:**
- картинки и видео (`--frames`), LoRA (`--config-json`, `loras[]`);
- `--remote --remote-url --remote-port 7859` — генерация на удалённом Draw Things gRPC-сервере;
- `--cloud-compute` — облако Draw Things;
- `train lora` — обучение LoRA.

**Прошлые замеры** (ZImageService, 25–26.09):
- DT на alex-neuro против Diffusers, 1344×1728: DT 33–41 с, Diffusers 13–14 с. Сэмплинг почти
  одинаковый, ≈12 с, разница — накладные расходы вызова.
- Hermes `image-studio` на Маке уже генерирует Z-Image через `draw-things-cli` (`~/.hermes/image-studio/config.json`).

**Место в архитектуре:** исполнитель картинок (Krea 2, Z-Image, LoRA) и запасной путь видео (LTX, Wan).
Работает на Маке сейчас: настоящий движок вместо имитации, пока alex-neuro недоступна. На alex-neuro
работает через gRPC-сервер по адресу. H3 остаётся на sglang или ComfyUI.
