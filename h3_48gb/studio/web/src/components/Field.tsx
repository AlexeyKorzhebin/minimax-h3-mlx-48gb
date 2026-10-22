import type { ReactNode } from "react";
import { Label } from "@/components/ui/label";

/** AB field: label line (text + mono hint) above the control. The control sits inside the Radix
 *  label, so `getByLabelText` and a click on the text both reach it. */
export function Field({ text, hint, children }: { text: string; hint?: string; children: ReactNode }) {
  return (
    <Label className="grid gap-1.5">
      <span className="flex items-baseline justify-between">
        {text}
        {hint && <small className="font-mono text-[10.5px] font-normal text-ink3">{hint}</small>}
      </span>
      {children}
    </Label>
  );
}
