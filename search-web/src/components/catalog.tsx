import { A } from "@solidjs/router";
import { AlertCircle, ArrowRight, ImageOff, Package, RotateCcw, Search, Sparkles } from "lucide-solid";
import { createEffect, createSignal, ErrorBoundary, For, on, Show, type JSX } from "solid-js";
import type { ProductHit } from "~/lib/types";
import { Badge, Button, Card, Skeleton, StatePanel } from "~/ui-kit";

export function DataBoundary(props: { children: JSX.Element; retry: () => Promise<unknown>; resetKey?: string }) {
  return <ErrorBoundary fallback={(error, reset) => {
    createEffect(on(() => props.resetKey, reset, { defer: true }));
    return <ErrorState error={error} retry={() => { void props.retry().then(reset, reset); }} />;
  }}>{props.children}</ErrorBoundary>;
}

export function ErrorState(props: { error: unknown; retry: () => void }) {
  const message = () => props.error instanceof Error ? props.error.message : String(props.error);
  return <StatePanel variant="error" title="Не удалось загрузить данные" description="Проверьте соединение и попробуйте ещё раз." icon={<AlertCircle size={25} />} action={<Button variant="outline" onClick={props.retry}><RotateCcw />Повторить</Button>}>
    <p class="error-message">{message()}</p>
  </StatePanel>;
}

export function EmptyState(props: { query?: string; reset?: () => void }) {
  return <StatePanel title={props.query ? "Пока ничего не нашлось" : "В этой категории пока нет товаров"} description={props.query ? "Попробуйте другие слова, более общий запрос или другой режим поиска." : "Выберите другую категорию или откройте весь каталог."} icon={<Search size={27} />} action={<Show when={props.reset}><Button variant="outline" onClick={props.reset}>Сбросить фильтры<ArrowRight /></Button></Show>} />;
}

export function ProductImage(props: { id: number; name: string; eager?: boolean }) {
  const [failed, setFailed] = createSignal(false);
  createEffect(on(() => props.id, () => setFailed(false), { defer: true }));
  return <Show when={!failed()} fallback={<ImagePlaceholder unavailable />}>
    <img src={`/api/images/${props.id}`} alt={props.name} loading={props.eager ? "eager" : "lazy"} decoding="async" onError={() => setFailed(true)} />
  </Show>;
}

export function ImagePlaceholder(props: { unavailable?: boolean }) {
  return <div class="image-placeholder">{props.unavailable ? <ImageOff size={34} strokeWidth={1.2} /> : <Package size={38} strokeWidth={1.2} />}<span>{props.unavailable ? "Изображение недоступно" : "Без изображения"}</span></div>;
}

export function ProductCard(props: { product: ProductHit; returnTo: string }) {
  const product = () => props.product;
  const href = () => `/products/${product().id}?from=${encodeURIComponent(props.returnTo)}`;
  return <Card class="product-card">
    <A class="product-image" href={href()} tabIndex={-1} aria-hidden="true">
      <Show when={product().imageIds.length} fallback={<ImagePlaceholder />}><ProductImage id={product().imageIds[0]} name={product().name} /></Show>
      <span class="product-id">СТЕ {product().id}</span>
    </A>
    <div class="product-content">
      <div class="product-category">{product().category || "Категория не указана"}</div>
      <h3><A href={href()}>{product().name}</A></h3>
      <p class="product-brand">{product().manufacturer || "Производитель не указан"}</p>
      <Show when={product().characteristics.length}><dl class="card-characteristics"><For each={product().characteristics.slice(0, 2)}>{item => <div><dt>{item.name}</dt><dd>{item.value}{item.unit ? ` ${item.unit}` : ""}</dd></div>}</For></dl></Show>
      <A href={href()} class="product-bottom" aria-label={`Подробнее: ${product().name}`}><span class="card-arrow" aria-hidden="true"><ArrowRight size={18} /></span></A>
      <Show when={product().score !== null && product().match !== "text" && product().match !== "catalog"}><div class="similarity" title="Косинусная близость векторов, не вероятность совпадения"><Sparkles size={12} />Семантическая близость {product().score!.toFixed(3)}<Show when={product().match === "both"}><Badge variant="secondary">+ текст</Badge></Show></div></Show>
    </div>
  </Card>;
}

export function ResultsSkeleton() {
  return <div role="status" aria-label="Загружаем товары"><div class="loading-label"><span class="loading-dot" />Загружаем каталог…</div><div class="product-grid" aria-hidden="true"><For each={[1, 2, 3, 4, 5, 6]}>{() => <div class="skeleton-card"><Skeleton class="skeleton-image" /><div class="skeleton-content"><Skeleton class="skeleton-line" /><Skeleton class="skeleton-line wide" /><Skeleton class="skeleton-line short" /></div></div>}</For></div></div>;
}
