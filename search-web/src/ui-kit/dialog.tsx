import type { ComponentProps, JSX, ValidComponent } from "solid-js";
import { splitProps } from "solid-js";
import * as DialogPrimitive from "@kobalte/core/dialog";
import type { PolymorphicProps } from "@kobalte/core/polymorphic";
import { cn } from "./utils";

const Dialog = DialogPrimitive.Root;
const DialogTrigger = DialogPrimitive.Trigger;
const DialogClose = DialogPrimitive.CloseButton;
const DialogPortal = DialogPrimitive.Portal;

const DialogOverlay = (props: ComponentProps<typeof DialogPrimitive.Overlay>) => {
  const [local, others] = splitProps(props, ["class"]);
  return <DialogPrimitive.Overlay class={cn("ui-dialog-overlay", local.class)} {...others} />;
};

type DialogContentProps<T extends ValidComponent = "div"> = DialogPrimitive.DialogContentProps<T> & {
  class?: string;
  children?: JSX.Element;
};

const DialogContent = <T extends ValidComponent = "div">(props: PolymorphicProps<T, DialogContentProps<T>>) => {
  const [local, others] = splitProps(props as DialogContentProps, ["class", "children"]);
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content class={cn("ui-dialog-content", local.class)} {...others}>
        {local.children}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
};

const DialogHeader = (props: JSX.HTMLAttributes<HTMLDivElement>) => {
  const [local, others] = splitProps(props, ["class"]);
  return <div class={cn("ui-dialog-header", local.class)} {...others} />;
};

const DialogFooter = (props: JSX.HTMLAttributes<HTMLDivElement>) => {
  const [local, others] = splitProps(props, ["class"]);
  return <div class={cn("ui-dialog-footer", local.class)} {...others} />;
};

const DialogTitle = (props: ComponentProps<typeof DialogPrimitive.Title>) => {
  const [local, others] = splitProps(props, ["class"]);
  return <DialogPrimitive.Title class={cn("ui-dialog-title", local.class)} {...others} />;
};

const DialogDescription = (props: ComponentProps<typeof DialogPrimitive.Description>) => {
  const [local, others] = splitProps(props, ["class"]);
  return <DialogPrimitive.Description class={cn("ui-dialog-description", local.class)} {...others} />;
};

export {
  Dialog,
  DialogTrigger,
  DialogClose,
  DialogPortal,
  DialogOverlay,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
};
