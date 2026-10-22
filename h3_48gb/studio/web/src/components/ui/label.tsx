// shadcn/ui label on Radix Label (new-york, shadcn@4.22.1), re-skinned: AB field label 12px/600.
import * as React from "react";
import { Label as LabelPrimitive } from "radix-ui";
import { cn } from "@/lib/utils";

function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn("text-xs font-semibold text-foreground select-none peer-disabled:opacity-50", className)}
      {...props}
    />
  );
}

export { Label };
