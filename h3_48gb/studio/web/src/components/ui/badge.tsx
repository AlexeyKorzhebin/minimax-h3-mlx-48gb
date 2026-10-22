// shadcn/ui badge (new-york, shadcn@4.22.1), re-skinned as the AB chip: 22px pill, mono 11px.
// `ok` and `warn` are AB additions; `default` is the cobalt chip, `outline` the neutral one.
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex h-[22px] w-fit shrink-0 items-center gap-1.5 overflow-hidden rounded-full border px-2 font-mono text-[11px] " +
    "whitespace-nowrap [&>svg]:pointer-events-none [&>svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-cobalt-line bg-cobalt-soft text-cobalt",
        outline: "border-border bg-background text-ink2",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        destructive: "border-transparent bg-danger-soft text-danger",
        ok: "border-transparent bg-ok-soft text-ok",
        warn: "border-transparent bg-warn-soft text-warn",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span";
  return <Comp data-slot="badge" data-variant={variant} className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
