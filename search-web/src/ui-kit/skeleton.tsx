import { splitProps, type Component, type ComponentProps } from "solid-js";
import { cn } from "./utils";

export type SkeletonProps = ComponentProps<"div">;

export const Skeleton: Component<SkeletonProps> = props => {
  const [local, others] = splitProps(props, ["class"]);
  return <div aria-hidden="true" class={cn("ui-skeleton rounded-sm", local.class)} {...others} />;
};
