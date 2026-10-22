// shadcn/ui button (new-york, `npx shadcn@4.22.1 add button`), re-skinned for design system AB:
// pills (form from direction C), the primary is a cobalt gradient with a glow, colours from the
// AB tokens mapped onto shadcn's variables in styles.css. Sizes follow AB: 32 / 46 px.
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-full border font-medium whitespace-nowrap " +
    "transition-[filter,transform,background-color] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40 " +
    "disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default:
          "border-transparent font-semibold text-primary-foreground shadow-[0_10px_24px_-12px_var(--cobalt)] " +
          "bg-[linear-gradient(135deg,var(--cobalt),color-mix(in_srgb,var(--cobalt)_70%,#5fa8ff))] " +
          "hover:brightness-105 hover:-translate-y-px",
        outline: "border-input bg-background text-foreground hover:bg-accent",
        secondary: "border-transparent bg-secondary text-secondary-foreground hover:bg-surface2",
        ghost: "border-transparent bg-transparent text-ink2 hover:bg-accent hover:text-foreground",
        destructive: "border-transparent bg-destructive text-white hover:brightness-95",
        link: "border-transparent text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-8 px-3 text-[13px]",
        lg: "h-[46px] px-[22px] text-sm",
        icon: "size-8",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  type = "button",
  ...props
}: React.ComponentProps<"button"> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "button";
  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      type={asChild ? undefined : type}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
