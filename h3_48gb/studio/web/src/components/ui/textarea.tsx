// shadcn/ui textarea (new-york, shadcn@4.22.1), re-skinned like Input; grows with its text
// (`field-sizing: content`, AB README «Спорно»: fixed height in old Safari/Firefox).
import * as React from "react";
import { cn } from "@/lib/utils";

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      rows={3}
      className={cn(
        "flex field-sizing-content min-h-[3.2em] w-full resize-y rounded-md border border-input bg-background px-2.5 py-2 " +
          "text-[13.5px] leading-[1.45] text-foreground outline-none placeholder:text-muted-foreground " +
          "disabled:cursor-not-allowed disabled:opacity-50 focus-visible:border-ring " +
          "focus-visible:shadow-[0_0_0_3px_var(--cobalt-soft)] aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
