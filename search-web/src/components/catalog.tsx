import { A } from "@solidjs/router";
import { AlertCircle, ArrowRight, ImageOff, Package, RotateCcw, Search, Sparkles } from "lucide-solid";
import { createEffect, createSignal, ErrorBoundary, For, on, Show, type JSX } from "solid-js";
import type { ProductHit } from "~/lib/types";
import { formatPrice } from "~/lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";

export function DataBoundary(props: { children: JSX.Element; retry: () => Promise<unknown>; resetKey?: string }) {
  return <ErrorBoundary fallback={(error, reset) => {
    createEffect(on(() => props.resetKey, reset, { defer: true }));
    return <ErrorState error={error} retry={() => { void props.retry().then(reset, reset); }} />;
  }}>{props.children}</ErrorBoundary>;
}

export function ErrorState(props: { error: unknown; retry: () => void }) {
  const message = () => props.error instanceof Error ? props.error.message : String(props.error);
  return <div class="state-panel error-panel" role="alert">
    <div class="state-icon"><AlertCircle size={25} /></div>
    <h3>Не удалось загрузить данные</h3>
    <p>Проверьте соединение и попробуйте ещё раз.</p>
    <p class="error-message">{message()}</p>
    <Button variant="outline" onClick={props.retry}><RotateCcw />Повторить</Button>
  </div>;
}

export function EmptyState(props: { query?: string; reset?: () => void }) {
  return <div class="state-panel">
    <div class="state-icon"><Search size={27} /></div>
    <h3>{props.query ? "Пока ничего не нашлось" : "В этой категории пока нет товаров"}</h3>
    <p>{props.query ? "Попробуйте другие слова, более общий запрос или другой режим поиска." : "Выберите другую категорию или откройте весь каталог."}</p>
    <Show when={props.reset}><Button variant="outline" onClick={props.reset}>Сбросить фильтры<ArrowRight /></Button></Show>
  </div>;
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
      <div class="product-bottom"><div><Show when={product().price !== null} fallback={<span class="missing-price">Цена не указана</span>}><strong class="product-price">{formatPrice(product().price!)}</strong></Show><Show when={product().unit}><span class="price-unit">за {product().unit}</span></Show></div><A href={href()} class="card-arrow" aria-label={`Подробнее: ${product().name}`}><ArrowRight size={18} /></A></div>
      <Show when={product().score !== null && product().match !== "text" && product().match !== "catalog"}><div class="similarity" title="Косинусная близость векторов, не вероятность совпадения"><Sparkles size={12} />Семантическая близость {product().score!.toFixed(3)}<Show when={product().match === "both"}><Badge variant="secondary">+ текст</Badge></Show></div></Show>
    </div>
  </Card>;
}

export function ResultsSkeleton() {
  return <div role="status" aria-label="Загружаем товары"><div class="loading-label"><span class="loading-dot" />Загружаем каталог…</div><div class="product-grid" aria-hidden="true"><For each={[1, 2, 3, 4, 5, 6]}>{() => <div class="skeleton-card"><div class="skeleton-image shimmer" /><div class="skeleton-content"><div class="skeleton-line shimmer" /><div class="skeleton-line wide shimmer" /><div class="skeleton-line short shimmer" /></div></div>}</For></div></div>;
}
