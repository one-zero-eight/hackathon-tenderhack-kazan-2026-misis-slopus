import { A } from "@solidjs/router";
import AlertCircle from "lucide-solid/icons/alert-circle";
import ArrowRight from "lucide-solid/icons/arrow-right";
import ImageOff from "lucide-solid/icons/image-off";
import Network from "lucide-solid/icons/network";
import Package from "lucide-solid/icons/package";
import RotateCcw from "lucide-solid/icons/rotate-ccw";
import Search from "lucide-solid/icons/search";
import { createEffect, createSignal, ErrorBoundary, For, on, Show, type JSX } from "solid-js";
import { apiImageUrl } from "~/lib/api";
import type { DirectoryTab, Product, ProductHit } from "~/lib/types";
import { Button, Card, Skeleton, StatePanel } from "~/ui-kit";

export function directoryHref(tab: DirectoryTab, code: string) {
  const resolved = tab === "spgz" ? "kpgz" : tab === "okpd" ? "ktru" : tab;
  return `/directories?tab=${resolved}&code=${encodeURIComponent(code)}`;
}

export function ktruTreeHref(code: string) {
  return directoryHref("ktru", code);
}

export function ProductClassifierLinks(props: { product: Pick<Product, "ktruCode" | "ktruName" | "okpd2" | "okpd2Name" | "kpgzCode" | "kpgzName" | "spgzId">; class?: string }) {
  return <div class={props.class ?? "classifier-links"}>
    <Show when={props.product.ktruCode} fallback={
      <Show when={props.product.okpd2}>{code =>
        <A class="ktru-link" href={directoryHref("ktru", code())} title="Открыть позиции КТРУ по ОКПД2 товара">
          <Network size={13} /><span>КТРУ</span>
          <span class="ktru-link-name">по ОКПД2 {code()}</span>
        </A>
      }</Show>
    }>{code =>
      <A class="ktru-link" href={directoryHref("ktru", code())} title="Открыть в справочнике КТРУ">
        <Network size={13} /><span>КТРУ {code()}</span>
        <Show when={props.product.ktruName}><span class="ktru-link-name">{props.product.ktruName}</span></Show>
      </A>
    }</Show>
    <Show when={props.product.okpd2}>{code =>
      <A href={directoryHref("okpd", code())} title="Открыть в справочнике ОКПД2">
        ОКПД2 {code()}
        <Show when={props.product.okpd2Name}><span class="ktru-link-name">— {props.product.okpd2Name}</span></Show>
      </A>
    }</Show>
    <Show when={props.product.kpgzCode}>{code =>
      <A href={directoryHref("kpgz", code())} title="Открыть в справочнике КПГЗ">
        КПГЗ {code()}
        <Show when={props.product.kpgzName}><span class="ktru-link-name">— {props.product.kpgzName}</span></Show>
      </A>
    }</Show>
    <Show when={props.product.spgzId}>{id =>
      <A href={directoryHref("spgz", String(id()))} title="Открыть в справочнике СПГЗ">СПГЗ {id()}</A>
    }</Show>
  </div>;
}

/** @deprecated use ProductClassifierLinks */
export function ProductKtruLink(props: { product: Pick<Product, "ktruCode" | "ktruName" | "okpd2" | "okpd2Name" | "kpgzCode" | "kpgzName" | "spgzId">; class?: string }) {
  return <ProductClassifierLinks product={props.product} class={props.class} />;
}

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
    <img src={apiImageUrl(props.id)} alt={props.name} loading={props.eager ? "eager" : "lazy"} decoding="async" onError={() => setFailed(true)} />
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
      <ProductClassifierLinks product={product()} class="classifier-links card-classifier-links" />
      <Show when={product().characteristics.length}><dl class="card-characteristics"><For each={product().characteristics.slice(0, 6)}>{item => <div><dt>{item.name}</dt><dd>{item.value}{item.unit ? ` ${item.unit}` : ""}</dd></div>}</For></dl></Show>
      <A href={href()} class="product-bottom" aria-label={`Подробнее: ${product().name}`}><span class="card-arrow" aria-hidden="true"><ArrowRight size={18} /></span></A>
    </div>
  </Card>;
}

export function ResultsSkeleton() {
  return <div role="status" aria-label="Загружаем товары"><div class="loading-label"><span class="loading-dot" />Загружаем каталог…</div><div class="product-grid" aria-hidden="true"><For each={[1, 2, 3, 4, 5, 6]}>{() => <div class="skeleton-card"><Skeleton class="skeleton-image" /><div class="skeleton-content"><Skeleton class="skeleton-line" /><Skeleton class="skeleton-line wide" /><Skeleton class="skeleton-line short" /></div></div>}</For></div></div>;
}
