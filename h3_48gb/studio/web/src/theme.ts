export type ThemeChoice = "light" | "dark" | "system";
const KEY = "studio.theme";
const ORDER: ThemeChoice[] = ["light", "dark", "system"];
const DARK_QUERY = "(prefers-color-scheme: dark)";

/** Light is the main theme (spec §4.4), so it is also the default. */
export function readTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(choice: ThemeChoice) {
  const dark = choice === "dark" || (choice === "system" && matchMedia(DARK_QUERY).matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    /* private mode: the choice lives until reload */
  }
}

/** While the choice is "system", follow the OS switching light/dark without a reload. */
export function followSystem(choice: ThemeChoice): () => void {
  if (choice !== "system") return () => {};
  const query = matchMedia(DARK_QUERY);
  const onChange = () => applyTheme("system");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function nextTheme(choice: ThemeChoice): ThemeChoice {
  return ORDER[(ORDER.indexOf(choice) + 1) % ORDER.length];
}

export const THEME_LABEL: Record<ThemeChoice, string> = {
  light: "Светлая",
  dark: "Тёмная",
  system: "Как в системе",
};
