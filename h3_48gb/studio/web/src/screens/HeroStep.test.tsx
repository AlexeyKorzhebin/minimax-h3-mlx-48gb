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
});
