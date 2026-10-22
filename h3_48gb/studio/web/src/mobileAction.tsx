import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

/** Phone 390 px (spec §4.4): one main button pinned to the bottom — the main action of the step
 *  the owner is in right now. Steps register their action with a priority; the bar shows the
 *  highest: a scene in conflict (4) > an edited scene (3) > a hero being filled in (2) > a
 *  proposal to apply (1) > «Предложить сцены» (0); between equals, the card that got input
 *  last (every keystroke re-registers it through `touched`). On wider screens every step shows its own inline buttons instead. */
export type MobileAction = {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  priority: number;
  /** Bumped on every input; re-registering moves the action ahead of equal priorities. */
  touched?: number;
  /** A second choice shown next to the main one (the 409 conflict: «Взять с сервера»). */
  secondary?: { label: string; onClick: () => void };
};
type Registered = MobileAction & { at: number };

const PHONE_QUERY = "(max-width: 767px)";
const Ctx = createContext<((id: string, action: MobileAction | null) => void) | null>(null);

export function useIsPhone(): boolean {
  const [phone, setPhone] = useState(() => typeof matchMedia === "function" && matchMedia(PHONE_QUERY).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const query = matchMedia(PHONE_QUERY);
    const onChange = () => setPhone(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return phone;
}

export function MobileActionProvider({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<Record<string, Registered>>({});
  const [clock] = useState(() => ({ now: 0 }));
  const phone = useIsPhone();
  const register = useCallback((id: string, action: MobileAction | null) => {
    setActions((all) => {
      if (action === null) {
        if (!(id in all)) return all;
        const { [id]: _gone, ...rest } = all;
        return rest;
      }
      clock.now += 1;
      return { ...all, [id]: { ...action, at: clock.now } };
    });
  }, [clock]);
  const top = useMemo(
    () => Object.values(actions).sort((a, b) => b.priority - a.priority || b.at - a.at)[0] ?? null,
    [actions],
  );
  return (
    <Ctx.Provider value={register}>
      {children}
      {phone && top && (
        <div data-testid="mobile-action-bar" className="fixed inset-x-0 bottom-0 z-20 flex gap-2 border-t border-line bg-[var(--scrim)] p-3 backdrop-blur-md">
          <Button size="lg" className="flex-1" onClick={top.onClick} disabled={top.disabled}>
            {top.label}
          </Button>
          {top.secondary && (
            <Button size="lg" variant="outline" className="flex-1" onClick={top.secondary.onClick}>
              {top.secondary.label}
            </Button>
          )}
        </div>
      )}
    </Ctx.Provider>
  );
}

/** Register (or, with null, withdraw) this step's main action for the phone bar. */
export function useMobileAction(id: string, action: MobileAction | null) {
  const register = useContext(Ctx);
  const key = action
    ? `${action.label}|${action.priority}|${action.disabled ? 1 : 0}|${action.secondary?.label ?? ""}|${action.touched ?? 0}`
    : "";
  const latest = useLatest(action);
  useEffect(() => {
    if (!register) return;
    const now = latest.current;
    register(id, now ? {
      ...now,
      onClick: () => latest.current?.onClick(),
      secondary: now.secondary && { label: now.secondary.label, onClick: () => latest.current?.secondary?.onClick() },
    } : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [register, id, key]);
  useEffect(() => () => register?.(id, null), [register, id]);
}

function useLatest<T>(value: T) {
  const [ref] = useState(() => ({ current: value }));
  ref.current = value;
  return ref;
}
