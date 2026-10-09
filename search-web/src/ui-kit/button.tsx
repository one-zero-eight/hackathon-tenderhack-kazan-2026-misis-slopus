// Adapted from stefan-karger/solid-ui (MIT), apps/docs/src/registry/ui/button.tsx.
// https://github.com/stefan-karger/solid-ui/blob/main/apps/docs/src/registry/ui/button.tsx
import type { JSX, ValidComponent } from "solid-js";
import { splitProps } from "solid-js";
import * as ButtonPrimitive from "@kobalte/core/button";
import type { PolymorphicProps } from "@kobalte/core/polymorphic";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "./utils";

const buttonVariants = cva("ui-button inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0", {
  variants: {
    variant: {
      default: "bg-primary text-primary-foreground hover:bg-primary/90",
      outline: "border border-input bg-transparent hover:bg-accent",
      secondary: "bg-secondary text-secondary-foreground hover:bg-secondary/80",
      ghost: "hover:bg-accent",
      link: "text-primary underline-offset-4 hover:underline",
    },
    size: { default: "h-10 px-4 py-2", sm: "h-9 px-3 text-xs", lg: "h-12 px-6", icon: "size-10" },
  },
  defaultVariants: { variant: "default", size: "default" },
});
type ButtonProps<T extends ValidComponent = "button"> = ButtonPrimitive.ButtonRootProps<T> & VariantProps<typeof buttonVariants> & { class?: string; children?: JSX.Element };
const Button = <T extends ValidComponent = "button">(props: PolymorphicProps<T, ButtonProps<T>>) => {
  const [local, others] = splitProps(props as ButtonProps, ["variant", "size", "class"]);
  return <ButtonPrimitive.Root class={cn(buttonVariants({ variant: local.variant, size: local.size }), local.class)} {...others} />;
};
export { Button, buttonVariants };
export type { ButtonProps };
