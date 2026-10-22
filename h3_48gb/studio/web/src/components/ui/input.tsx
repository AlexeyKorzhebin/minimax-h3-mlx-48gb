// shadcn/ui input (new-york, shadcn@4.22.1), re-skinned: AB field — 1px line2 border, radius 6,
// 13.5px text, cobalt focus ring of cobalt-soft.
import * as React from "react";
import { cn } from "@/lib/utils";

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "w-full min-w-0 rounded-md border border-input bg-background px-2.5 py-2 text-[13.5px] leading-[1.45] text-foreground " +
          "outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50 " +
          "focus-visible:border-ring focus-visible:shadow-[0_0_0_3px_var(--cobalt-soft)] aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
