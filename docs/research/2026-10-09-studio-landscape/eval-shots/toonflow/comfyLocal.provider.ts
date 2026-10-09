const rules = [
  { type: "input", field: "baseUrl" as const, title: "ComfyUI URL", value: "http://host.docker.internal:8188", props: { placeholder: "http://host.docker.internal:8188" } },
  { type: "input", field: "checkpoint" as const, title: "Checkpoint", value: "sd_turbo.safetensors", props: { placeholder: "sd_turbo.safetensors" } },
] as const;

export default {
  id: "comfyLocal",
  label: "Local ComfyUI",
  version: "1.0.0",
  readme: "Local ComfyUI txt2img (SD-Turbo API workflow).",
  rules,
  models: [
    {
      id: "sd-turbo",
      label: "SD-Turbo (ComfyUI)",
      type: "image",
      mode: ["text"],
      imageSizes: ["512"],
      imageRatios: ["1:1", "16:9", "9:16"],
    },
  ] satisfies ProviderModel[],
  async generateImage(request: ImageRequest): Promise<MediaAsset[]> {
    const base = (this.config.baseUrl?.trim() || "http://host.docker.internal:8188").replace(/\/$/, "");
    const signal = AbortSignal.any([AbortSignal.timeout(5 * 60_000), ...(this.signal ? [this.signal] : [])]);
    const dims: Record<string, [number, number]> = { "1:1": [512, 512], "16:9": [640, 384], "9:16": [384, 640] };
    const [width, height] = dims[request.ratio ?? "1:1"] ?? [512, 512];
    const workflow = {
      "4": { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: this.config.checkpoint } },
      "5": { class_type: "EmptyLatentImage", inputs: { width, height, batch_size: 1 } },
      "6": { class_type: "CLIPTextEncode", inputs: { text: request.prompt, clip: ["4", 1] } },
      "7": { class_type: "CLIPTextEncode", inputs: { text: "", clip: ["4", 1] } },
      "3": { class_type: "KSampler", inputs: { seed: Math.floor(Math.random() * 2 ** 31), steps: 2, cfg: 1, sampler_name: "euler_ancestral", scheduler: "normal", denoise: 1, model: ["4", 0], positive: ["6", 0], negative: ["7", 0], latent_image: ["5", 0] } },
      "8": { class_type: "VAEDecode", inputs: { samples: ["3", 0], vae: ["4", 2] } },
      "9": { class_type: "SaveImage", inputs: { filename_prefix: "toonflow", images: ["8", 0] } },
    };
    const submit = await this.tool.fetch(`${base}/prompt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: workflow }), signal });
    if (!submit.ok) throw new Error(`ComfyUI /prompt HTTP ${submit.status}: ${await submit.text()}`);
    const promptId = ((await submit.json()) as { prompt_id?: string }).prompt_id;
    if (!promptId) throw new Error("ComfyUI: no prompt_id");
    while (true) {
      signal.throwIfAborted();
      const h = await this.tool.fetch(`${base}/history/${promptId}`, { signal });
      const hist = (await h.json()) as Record<string, { outputs?: Record<string, { images?: { filename: string; subfolder: string; type: string }[] }>; status?: { status_str?: string } }>;
      const entry = hist[promptId];
      if (entry?.status?.status_str === "error") throw new Error("ComfyUI: execution error");
      const img = entry?.outputs?.["9"]?.images?.[0];
      if (img) {
        const view = await this.tool.fetch(`${base}/view?filename=${encodeURIComponent(img.filename)}&subfolder=${encodeURIComponent(img.subfolder)}&type=${img.type}`, { signal });
        if (!view.ok) throw new Error(`ComfyUI /view HTTP ${view.status}`);
        return [{ mediaType: "image", type: "binary", data: new Uint8Array(await view.arrayBuffer()), mimeType: "image/png" }];
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  },
} satisfies ProviderDefinition<typeof rules>;
