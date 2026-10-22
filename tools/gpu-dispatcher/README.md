# gpu-dispatcher

Хостовая служба панели на alex-neuro (спека §3.4). Только stdlib Python, слушает `127.0.0.1:8790`.
Единственная поднимает и гасит свои движки: H3 (sglang `ref2va`, :30020) и ComfyUI LTX (:8188).
Запускает их так же, как `h3-bench/pipeline.sh`, но гасит только своё: SIGTERM группе своего
процесса, ждёт до 120 с, затем SIGKILL группе. Никакого `pkill -f`.

## Правило «чужое не гасится»
Своё = pid из `state.json` И его `/proc/<pid>/cmdline` содержит все маркеры движка (для H3 это
`sglang`, `serve`, `--model-variant ref2va`, `--port 30020`). Всё остальное на GPU (nvidia-smi)
чужое: диспетчер ждёт его и показывает в `/status`, но не трогает. Qwen гасится и возвращается
только по явной просьбе (`/qwen/unload`, `/qwen/restore`).

## generation.lock
`flock` на `qwen-image21-lab/generation.lock` берётся при запуске движка, fd наследует сам движок,
поэтому замок живёт, пока жив движок, даже при перезапуске диспетчера.

## Ручки
`GET /status`, `POST /acquire {"engine": "h3"|"ltx"|"comfy-studio", "client": "…"}`,
`POST /release`, `POST /qwen/unload`, `POST /qwen/restore`.

## comfy-studio (студия, спека студии §8.4)
ComfyUI студии: `~/Projects/comfy-h3` (v0.42, H3 и Krea 2), порт 8189, вывод
`/home/alex/Outputs/comfy-h3`, лог `~/Projects/comfy-h3/logs/comfy-studio-*.log`. Клиент — `studio-worker`.
Диспетчер держит на карте один движок: пока ComfyUI панели (`ltx`) или H3 занят клиентом
`panel-worker`, студия ждёт (`wait`, «карту держит … клиента panel-worker»), и наоборот.

## Установка
    sudo cp h3-gpu-dispatcher.service /etc/systemd/system/ && sudo systemctl daemon-reload \
      && sudo systemctl enable --now h3-gpu-dispatcher

Состояние: `~/.local/state/h3-gpu-dispatcher/state.json` (или `$H3_DISPATCHER_STATE`).
`KillMode=process` в юните: перезапуск службы не убивает запущенные ею движки.
