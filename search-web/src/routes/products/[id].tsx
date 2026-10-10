import { Meta, Title } from "@solidjs/meta";
import { A, createAsync, revalidate, useParams, useSearchParams } from "@solidjs/router";
import { HttpStatusCode } from "@solidjs/start";
import { ArrowLeft, ArrowUpRight, ChevronRight, FileText, Globe2, Package, Sparkles, Tags } from "lucide-solid";
import { createSignal, For, Show, Suspense } from "solid-js";
import { DataBoundary, ImagePlaceholder, ProductCard, ProductClassifierLinks, ProductImage, ResultsSkeleton } from "~/components/catalog";
import { buttonVariants, Skeleton } from "~/ui-kit";
import type { Product } from "~/lib/types";
import { getProduct, getSimilarProducts } from "~/lib/rpc";
import { formatDate, formatNumber } from "~/lib/utils";

function Gallery(props: { product: Product }) {
  const [selected, setSelected] = createSignal(0);
  return <div class="product-gallery"><div class="gallery-main"><Show when={props.product.imageIds.length} fallback={<ImagePlaceholder />}><ProductImage id={props.product.imageIds[selected()] ?? props.product.imageIds[0]} name={props.product.name} eager /></Show><span class="gallery-caption">СТЕ {props.product.id}</span></div><Show when={props.product.imageIds.length > 1}><div class="gallery-thumbnails" aria-label="Изображения товара"><For each={props.product.imageIds}>{(id, index) => <button classList={{ selected: selected() === index() }} type="button" aria-label={`Изображение ${index() + 1}`} aria-pressed={selected() === index()} onClick={() => setSelected(index())}><ProductImage id={id} name={`${props.product.name}, изображение ${index() + 1}`} /></button>}</For></div></Show></div>;
}

function SimilarProducts(props: { id: number; back: string }) {
  const similar = createAsync(() => getSimilarProducts(props.id));
  return <Show when={similar()}>{items => <Show when={items().length} fallback={<p class="no-characteristics">Похожие товары пока не найдены.</p>}><div class="product-grid"><For each={items()}>{product => <ProductCard product={product} returnTo={props.back} />}</For></div></Show>}</Show>;
}

function Details(props: { id: number; back: string }) {
  const product = createAsync(() => Number.isSafeInteger(props.id) && props.id > 0 ? getProduct(props.id) : Promise.resolve(null));
  return <Show when={product() !== undefined}><Show when={product()} keyed fallback={<section class="state-panel detail-not-found"><HttpStatusCode code={404} /><Package size={38} strokeWidth={1.3} /><h1>Товар не найден</h1><p>Карточки с таким идентификатором нет в загруженном каталоге.</p><A class={buttonVariants({ variant: "outline" })} href={props.back}>Вернуться к поиску<ArrowLeft size={16} /></A></section>}>{item => <>
    <Title>{item.name} — Каталог</Title><Meta name="description" content={`${item.name}. Характеристики и сведения из Портала поставщиков Москвы.`} />
    <nav class="breadcrumbs" aria-label="Хлебные крошки"><A href={props.back}>Каталог</A><Show when={item.category}><ChevronRight size={14} /><A href={`/catalog?category=${item.categoryId}`}>{item.category}</A></Show></nav>
    <section class="product-detail"><Gallery product={item} /><div class="detail-summary"><div class="detail-category">{item.category || "Категория не указана"}</div><h1>{item.name}</h1><p class="detail-manufacturer">{item.manufacturer || "Производитель не указан"}<Show when={item.country}><span class="detail-country"><Globe2 size={14} />{item.country}</span></Show></p>
      <dl class="detail-facts"><div><dt>ID товарной единицы</dt><dd>{item.id}</dd></div><Show when={item.modelName}><div><dt>Модель</dt><dd>{item.modelName}</dd></div></Show><Show when={item.partNumber}><div><dt>Артикул</dt><dd>{item.partNumber}</dd></div></Show><Show when={item.unit}><div><dt>Единица измерения</dt><dd>{item.unit}</dd></div></Show><Show when={item.sourceUpdatedAt}><div><dt>Обновление в источнике</dt><dd>{formatDate(item.sourceUpdatedAt)}</dd></div></Show></dl>
      <a class={`${buttonVariants({ size: "lg" })} original-link`} href={`https://zakupki.mos.ru/sku/view/${item.id}`} target="_blank" rel="noopener noreferrer">Открыть на Портале поставщиков<ArrowUpRight size={18} /></a>
    </div></section>
    <Show when={item.ktruCode || item.okpd2 || item.okpd2Name || item.kpgzCode || item.kpgzName || item.spgzId}><section class="classification-section" aria-labelledby="classification-heading"><div class="section-heading"><Tags size={21} strokeWidth={1.6} /><h2 id="classification-heading">Классификация</h2></div><ProductClassifierLinks product={item} class="classifier-links detail-classifier-links" /></section></Show>
    <div class="detail-sections"><section class="characteristics-section"><div class="section-heading"><Tags size={21} strokeWidth={1.6} /><h2>Характеристики</h2><span>{formatNumber(item.characteristics.filter(attribute => attribute.name.toLocaleLowerCase("ru") !== "ктру").length)}</span></div><Show when={item.characteristics.some(attribute => attribute.name.toLocaleLowerCase("ru") !== "ктру")} fallback={<p class="no-characteristics">В загруженных данных характеристики не указаны.</p>}><dl class="characteristics-table"><For each={item.characteristics.filter(attribute => attribute.name.toLocaleLowerCase("ru") !== "ктру")}>{attribute => <div><dt>{attribute.name}</dt><dd>{attribute.value || "Не указано"}{attribute.unit ? ` ${attribute.unit}` : ""}</dd></div>}</For></dl></Show></section>
      <Show when={item.description}><aside class="detail-aside"><section class="description-section"><div class="section-heading"><FileText size={20} strokeWidth={1.6} /><h2>Описание</h2></div><p>{item.description}</p></section></aside></Show>
    </div>
    <section class="similar-products" aria-labelledby="similar-products-heading"><div class="section-heading"><Sparkles size={21} strokeWidth={1.6} /><h2 id="similar-products-heading">Похожие товары</h2></div><DataBoundary resetKey={String(item.id)} retry={() => revalidate(getSimilarProducts.key)}><Suspense fallback={<ResultsSkeleton />}><SimilarProducts id={item.id} back={props.back} /></Suspense></DataBoundary></section>
  </>}</Show></Show>;
}

export default function ProductPage() {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const back = () => {
    const from = Array.isArray(searchParams.from) ? searchParams.from[0] : searchParams.from;
    return from === "/catalog" || from?.startsWith("/catalog?") ? from : "/catalog";
  };
  return <main id="main-content" class="page-width detail-main"><A href={back()} class="back-link"><ArrowLeft size={16} />К результатам поиска</A><DataBoundary resetKey={params.id} retry={() => revalidate(getProduct.key)}><Suspense fallback={<div class="detail-loading" role="status"><Skeleton class="skeleton-image" /><div><Skeleton class="skeleton-line wide" /><Skeleton class="skeleton-line" /><p>Загружаем карточку товара…</p></div></div>}><Details id={Number(params.id)} back={back()} /></Suspense></DataBoundary></main>;
}
