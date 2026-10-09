import { children, Show, splitProps, type Component, type ComponentProps, type JSX } from "solid-js";
import { cn } from "./utils";

export type StatePanelProps = Omit<ComponentProps<"div">, "title"> & {
  title: JSX.Element;
  description?: JSX.Element;
  icon?: JSX.Element;
  action?: JSX.Element;
  variant?: "default" | "error";
};

export const StatePanel: Component<StatePanelProps> = props => {
  const [local, others] = splitProps(props, ["class", "title", "description", "icon", "action", "variant", "children"]);
  // Resolve JSX slots once: checking and rendering a getter twice can duplicate hydration IDs.
  const icon = children(() => local.icon);
  const description = children(() => local.description);
  return <div role={local.variant === "error" ? "alert" : undefined} class={cn("state-panel", local.variant === "error" && "error-panel", local.class)} {...others}>
    <Show when={icon()}><div class="state-icon">{icon()}</div></Show>
    <h3>{local.title}</h3>
    <Show when={description()}><p>{description()}</p></Show>
    {local.children}
    {local.action}
  </div>;
};
