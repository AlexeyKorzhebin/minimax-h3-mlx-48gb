import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { stubFetch } from "@/test/fetchStub";
import { snapshot } from "@/test/snapshot";
import { HeroStep } from "./HeroStep";

const ASSET = { id: "a_1", kind: "image", sha256: "h", bytes: 9, mime: "image/png", url: "/api/studio/assets/a_1/file" };

describe("HeroStep", () => {
  it("uploads the photo and saves the hero with it and the project revision", async () => {
    const calls = stubFetch({
      "POST /api/studio/assets": { status: 201, body: ASSET },
      "POST /api/studio/projects/p_1/cast": { status: 201, body: {} },
    });
    const onChanged = vi.fn();
    render(<HeroStep snap={snapshot()} onChanged={onChanged} />);
    await userEvent.upload(screen.getByLabelText("Фото героя"), new File(["png"], "h.png", { type: "image/png" }));
    await screen.findByAltText("Фото героя");
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина с косой");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.map((c) => c.key)).toEqual(["POST /api/studio/assets", "POST /api/studio/projects/p_1/cast"]);
    expect(calls[1].body).toEqual({
      expected_revision: 3, tag: "anna", description_ru: "женщина с косой",
      assets: [{ asset_id: "a_1", role: "image" }],
    });
  });

  it("when the LLM is down, asks for the English card and sends it", async () => {
    const calls = stubFetch({
      "POST /api/studio/projects/p_1/cast": [
        { status: 503, body: { error: { code: "llm_not_configured", message: "Сценарист не настроен", detail: {} } } },
        { status: 201, body: {} },
      ],
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Сценарист не настроен. Впишите английское описание сами.");
    await userEvent.type(screen.getByLabelText(/Описание по-английски/), "A woman.");
    await userEvent.type(screen.getByLabelText(/Портретная фраза/), "a woman with a braid");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toEqual({
      expected_revision: 3, tag: "anna", description_ru: "женщина", assets: [],
      description_en: "A woman.", portrait_phrase: "a woman with a braid",
    });
  });

  it("shows the server's message for a photo over 50 MP (413) and keeps the form usable", async () => {
    stubFetch({
      "POST /api/studio/assets": { status: 413, body: { error: { code: "asset_pixels_too_large", message: "Картинка больше 50 мегапикселей", detail: {} } } },
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.upload(screen.getByLabelText("Фото героя"), new File(["png"], "h.png", { type: "image/png" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Картинка больше 50 мегапикселей");
    expect(screen.queryByAltText("Фото героя")).toBeNull();
  });

  it("shows the server's message for a half-filled manual card (400 cast_card_invalid) and does not open anything else", async () => {
    stubFetch({
      "POST /api/studio/projects/p_1/cast": { status: 400, body: { error: { code: "cast_card_invalid", message: "Заполните обе части карточки: описание и портретную фразу", detail: {} } } },
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Заполните обе части карточки: описание и портретную фразу");
    expect(screen.queryByLabelText(/Описание по-английски/)).toBeNull();
  });

  it("r1 №7: «GPU занят» (409 gpu_busy) from the scenarist opens the English card, like a dead LLM", async () => {
    stubFetch({
      "POST /api/studio/projects/p_1/cast": { status: 409, body: { error: { code: "gpu_busy", message: "GPU занят", detail: {} } } },
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    expect((await screen.findByRole("alert")).textContent).toBe("GPU занят. Впишите английское описание сами.");
    expect(screen.getByLabelText(/Описание по-английски/)).toBeTruthy();
  });

  it("r1 №7: a revision conflict reloads the snapshot and says so", async () => {
    stubFetch({
      "POST /api/studio/projects/p_1/cast": { status: 409, body: { error: { code: "revision_conflict", message: "x", detail: {} } } },
    });
    const onChanged = vi.fn();
    render(<HeroStep snap={snapshot()} onChanged={onChanged} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Проект изменился в другом окне — данные обновлены, сохраните ещё раз");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("r1 №7: after a save the form is empty again — photo, tag and description", async () => {
    stubFetch({
      "POST /api/studio/assets": { status: 201, body: ASSET },
      "POST /api/studio/projects/p_1/cast": { status: 201, body: {} },
    });
    const onChanged = vi.fn();
    render(<HeroStep snap={snapshot()} onChanged={onChanged} />);
    await userEvent.upload(screen.getByLabelText("Фото героя"), new File(["png"], "h.png", { type: "image/png" }));
    await screen.findByAltText("Фото героя");
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(screen.queryByAltText("Фото героя")).toBeNull();
    expect([(screen.getByLabelText(/Тег/) as HTMLInputElement).value, (screen.getByLabelText(/Кто это/) as HTMLTextAreaElement).value]).toEqual(["", ""]);
  });

  it("r1 №3: text typed while the save is in flight survives the answer; what was sent is cleared", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const calls = stubFetch({
      "POST /api/studio/projects/p_1/cast": async () => { await held; return { status: 201, body: {} }; },
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "first");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    await userEvent.type(screen.getByLabelText(/Кто это/), " second");
    release();
    await vi.waitFor(() => expect((screen.getByLabelText(/Тег/) as HTMLInputElement).value).toBe(""));
    expect((screen.getByLabelText(/Кто это/) as HTMLTextAreaElement).value).toBe("first second");
    expect((calls[0].body as { description_ru: string }).description_ru).toBe("first");
  });

  it("r1 №3: «Сохранить» waits for the photo upload; of two chosen photos the last chosen wins, however the answers arrive", async () => {
    let releaseA!: () => void;
    const heldA = new Promise<void>((r) => (releaseA = r));
    const asset = (id: string) => ({ ...ASSET, id, url: `/api/studio/assets/${id}/file` });
    const calls = stubFetch({
      "POST /api/studio/assets": [
        async () => { await heldA; return { status: 201, body: asset("a_A") }; },
        { status: 201, body: asset("a_B") },
      ],
      "POST /api/studio/projects/p_1/cast": { status: 201, body: {} },
    });
    render(<HeroStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await userEvent.type(screen.getByLabelText(/Кто это/), "женщина");
    const input = screen.getByLabelText("Фото героя");
    await userEvent.upload(input, new File(["a"], "a.png", { type: "image/png" }));
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Сохранить героя" }).disabled).toBe(true);   // A is still uploading
    await userEvent.upload(input, new File(["b"], "b.png", { type: "image/png" }));
    await vi.waitFor(() => expect(screen.getByAltText("Фото героя").getAttribute("src")).toBe("/api/studio/assets/a_B/file"));
    releaseA();                                                                      // the older answer arrives last
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByAltText("Фото героя").getAttribute("src")).toBe("/api/studio/assets/a_B/file");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить героя" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "POST /api/studio/projects/p_1/cast")).toBe(true));
    expect((calls.find((c) => c.key === "POST /api/studio/projects/p_1/cast")!.body as { assets: unknown }).assets).toEqual([{ asset_id: "a_B", role: "image" }]);
  });
});
