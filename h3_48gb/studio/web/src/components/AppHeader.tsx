import { Clapperboard, Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { applyTheme, followSystem, nextTheme, readTheme, THEME_LABEL, type ThemeChoice } from "@/theme";

const ICON: Record<ThemeChoice, typeof Sun> = { light: Sun, dark: Moon, system: Monitor };

export function AppHeader() {
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  useEffect(() => followSystem(theme), [theme]);
  const Icon = ICON[theme];
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-[var(--scrim)] backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-[1360px] items-center gap-3 px-4 md:px-8">
        <a href="#/" className="flex items-center gap-2.5 text-ink no-underline">
          <span className="grid h-6 w-6 place-items-center rounded-[5px] bg-ink text-bg">
            <Clapperboard className="h-3.5 w-3.5" />
          </span>
          <span className="h-display text-[17px]">Студия</span>
        </a>
        <div className="ml-auto">
          <Button
            variant="outline"
            aria-label={`Тема: ${THEME_LABEL[theme]}`}
            onClick={() => {
              const next = nextTheme(theme);
              applyTheme(next);
              setTheme(next);
            }}
          >
            <Icon />
            <span className="hidden sm:inline">{THEME_LABEL[theme]}</span>
          </Button>
        </div>
      </div>
    </header>
  );
}
