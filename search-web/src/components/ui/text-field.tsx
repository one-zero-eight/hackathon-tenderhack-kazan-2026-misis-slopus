// Adapted from stefan-karger/solid-ui (MIT), apps/docs/src/registry/ui/text-field.tsx.
// https://github.com/stefan-karger/solid-ui/blob/main/apps/docs/src/registry/ui/text-field.tsx
import { splitProps, type ValidComponent } from "solid-js";
import type { PolymorphicProps } from "@kobalte/core/polymorphic";
import * as TextFieldPrimitive from "@kobalte/core/text-field";
import { cn } from "~/lib/utils";
type RootProps<T extends ValidComponent = "div"> = TextFieldPrimitive.TextFieldRootProps<T> & { class?: string };
const TextField = <T extends ValidComponent = "div">(props: PolymorphicProps<T, RootProps<T>>) => {
  const [local, others] = splitProps(props as RootProps, ["class"]);
  return <TextFieldPrimitive.Root class={cn("flex flex-col gap-1", local.class)} {...others} />;
};
type InputProps<T extends ValidComponent = "input"> = TextFieldPrimitive.TextFieldInputProps<T> & { class?: string; type?: "text" | "search" };
const TextFieldInput = <T extends ValidComponent = "input">(props: PolymorphicProps<T, InputProps<T>>) => {
  const [local, others] = splitProps(props as InputProps, ["class", "type"]);
  return <TextFieldPrimitive.Input type={local.type ?? "text"} class={cn("flex h-10 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50", local.class)} {...others} />;
};
type LabelProps<T extends ValidComponent = "label"> = TextFieldPrimitive.TextFieldLabelProps<T> & { class?: string };
const TextFieldLabel = <T extends ValidComponent = "label">(props: PolymorphicProps<T, LabelProps<T>>) => {
  const [local, others] = splitProps(props as LabelProps, ["class"]);
  return <TextFieldPrimitive.Label class={cn("text-sm font-medium", local.class)} {...others} />;
};
export { TextField, TextFieldInput, TextFieldLabel };
