// Adapted from stefan-karger/solid-ui (MIT), apps/docs/src/registry/ui/badge.tsx.
// https://github.com/stefan-karger/solid-ui/blob/main/apps/docs/src/registry/ui/badge.tsx
import { splitProps, type Component, type ComponentProps } from "solid-js";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "./utils";
const badgeVariants = cva("inline-flex items-center rounded-none border px-2.5 py-1 text-xs font-medium", {
  variants: { variant: {
    default: "border-transparent bg-primary text-primary-foreground",
    secondary: "border-transparent bg-secondary text-secondary-foreground",
    outline: "border-input text-muted-foreground",
  } },
  defaultVariants: { variant: "default" },
});
type BadgeProps = ComponentProps<"div"> & VariantProps<typeof badgeVariants>;
const Badge: Component<BadgeProps> = props => {
  const [local, others] = splitProps(props, ["class", "variant"]);
  return <div class={cn(badgeVariants({ variant: local.variant }), local.class)} {...others} />;
};
export { Badge, badgeVariants };
export type { BadgeProps };
