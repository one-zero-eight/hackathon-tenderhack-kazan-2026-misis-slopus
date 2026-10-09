// Adapted from stefan-karger/solid-ui (MIT), apps/docs/src/registry/ui/text-field.tsx.
// https://github.com/stefan-karger/solid-ui/blob/main/apps/docs/src/registry/ui/text-field.tsx
import { splitProps, type ValidComponent } from "solid-js";
import type { PolymorphicProps } from "@kobalte/core/polymorphic";
import * as TextFieldPrimitive from "@kobalte/core/text-field";
import { cn } from "./utils";
type TextFieldProps<T extends ValidComponent = "div"> = TextFieldPrimitive.TextFieldRootProps<T> & { class?: string };
const TextField = <T extends ValidComponent = "div">(props: PolymorphicProps<T, TextFieldProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldProps, ["class"]);
  return <TextFieldPrimitive.Root class={cn("flex flex-col gap-1", local.class)} {...others} />;
};
type TextFieldInputProps<T extends ValidComponent = "input"> = TextFieldPrimitive.TextFieldInputProps<T> & { class?: string; type?: "text" | "search" };
const TextFieldInput = <T extends ValidComponent = "input">(props: PolymorphicProps<T, TextFieldInputProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldInputProps, ["class", "type"]);
  return <TextFieldPrimitive.Input type={local.type ?? "text"} class={cn("flex h-10 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50 data-[invalid]:border-destructive", local.class)} {...others} />;
};
type TextFieldTextareaProps<T extends ValidComponent = "textarea"> = TextFieldPrimitive.TextFieldTextAreaProps<T> & { class?: string };
const TextFieldTextarea = <T extends ValidComponent = "textarea">(props: PolymorphicProps<T, TextFieldTextareaProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldTextareaProps, ["class"]);
  return <TextFieldPrimitive.TextArea class={cn("flex min-h-28 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50 data-[invalid]:border-destructive", local.class)} {...others} />;
};
type TextFieldLabelProps<T extends ValidComponent = "label"> = TextFieldPrimitive.TextFieldLabelProps<T> & { class?: string };
const TextFieldLabel = <T extends ValidComponent = "label">(props: PolymorphicProps<T, TextFieldLabelProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldLabelProps, ["class"]);
  return <TextFieldPrimitive.Label class={cn("text-sm font-medium", local.class)} {...others} />;
};
type TextFieldDescriptionProps<T extends ValidComponent = "div"> = TextFieldPrimitive.TextFieldDescriptionProps<T> & { class?: string };
const TextFieldDescription = <T extends ValidComponent = "div">(props: PolymorphicProps<T, TextFieldDescriptionProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldDescriptionProps, ["class"]);
  return <TextFieldPrimitive.Description class={cn("text-xs text-muted-foreground", local.class)} {...others} />;
};
type TextFieldErrorMessageProps<T extends ValidComponent = "div"> = TextFieldPrimitive.TextFieldErrorMessageProps<T> & { class?: string };
const TextFieldErrorMessage = <T extends ValidComponent = "div">(props: PolymorphicProps<T, TextFieldErrorMessageProps<T>>) => {
  const [local, others] = splitProps(props as TextFieldErrorMessageProps, ["class"]);
  return <TextFieldPrimitive.ErrorMessage class={cn("text-xs text-destructive", local.class)} {...others} />;
};
export { TextField, TextFieldInput, TextFieldTextarea, TextFieldLabel, TextFieldDescription, TextFieldErrorMessage };
export type { TextFieldProps, TextFieldInputProps, TextFieldTextareaProps, TextFieldLabelProps, TextFieldDescriptionProps, TextFieldErrorMessageProps };
