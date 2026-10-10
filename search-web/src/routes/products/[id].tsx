import { Meta, Title } from "@solidjs/meta";
import { A, createAsync, revalidate, useParams, useSearchParams } from "@solidjs/router";
import { HttpStatusCode } from "@solidjs/start";
import ArrowLeft from "lucide-solid/icons/arrow-left";
import ArrowUpRight from "lucide-solid/icons/arrow-up-right";
import ChevronRight from "lucide-solid/icons/chevron-right";
import FileText from "lucide-solid/icons/file-text";
import Globe2 from "lucide-solid/icons/globe-2";
import Package from "lucide-solid/icons/package";
import Sparkles from "lucide-solid/icons/sparkles";
import Tags from "lucide-solid/icons/tags";
import { createSignal, For, Show, Suspense } from "solid-js";
import { DataBoundary, ImagePlaceholder, ProductCard, ProductImage, ResultsSkeleton } from "~/components/catalog";
import { CategoryPairs } from "~/components/category-pairs";
import { buttonVariants, Skeleton } from "~/ui-kit";
import { characteristicSourceLabel } from "~/lib/merge-characteristics";
import type { Characteristic, DraftCharacteristic, Product } from "~/lib/types";
import { getClassifierCharacteristicNames, getProduct, getSimilarProducts, resolveProductCategoryVotes } from "~/lib/rpc";
import { formatDate, formatNumber } from "~/lib/utils";

function normCharName(name: string): string {
  return name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
}

function isKtruMetaChar(name: string): boolean {
  const n = normCharName(name);
  return n === "ктру" || n.startsWith("ктру ");
}

function ProductClassification(props: { product: Product }) {
  const votes = createAsync(() => resolveProductCategoryVotes({
    okpd2: props.product.okpd2 || null,
    okpd2Name: props.product.okpd2Name || null,
    ktruCode: props.product.ktruCode || null,
    ktruName: props.product.ktruName || null,
    kpgzCode: props.product.kpgzCode || null,
    kpgzName: props.product.kpgzName || null,
    spgzId: props.product.spgzId,
    characteristics: props.product.characteristics.map((item) => ({ name: item.name, value: item.value })),
  }));
  return (
    <section class="classification-section" aria-labelledby="classification-heading">
      <div class="section-heading"><Tags size={21} strokeWidth={1.6} /><h2 id="classification-heading">Классификация</h2></div>
      <Show when={votes()} fallback={<div class="create-vote-card create-vote-card-skeleton" aria-hidden="true" />}>
        {(data) =>
          <CategoryPairs
            okpd2={data().okpd2}
            ktru={data().ktru}
            kpgz={data().kpgz}
            spgz={data().spgz}
          />
        }
      </Show>
    </section>
  );
}

function ProductCharacteristics(props: { product: Product }) {
  const rows = () => props.product.characteristics.filter((item) => !isKtruMetaChar(item.name));
  const classifierNames = createAsync(async () => {
    const lists = await getClassifierCharacteristicNames({
      ktruCode: props.product.ktruCode || null,
      okpd2: props.product.okpd2 || null,
      spgzCode: props.product.spgzId != null ? String(props.product.spgzId) : null,
      kpgzCode: props.product.kpgzCode || null,
    });
    return {
      ktru: new Set(lists.ktru),
      spgz: new Set(lists.spgz),
    };
  });
  const sourceFor = (item: Characteristic): DraftCharacteristic["source"] => {
    const names = classifierNames();
    if (!names) return "custom";
    const key = normCharName(item.name);
    const inKtru = names.ktru.has(key);
    const inSpgz = names.spgz.has(key);
    if (inKtru && inSpgz) return "both";
    if (inKtru) return "ktru";
    if (inSpgz) return "spgz";
    return "custom";
  };
  return (
    <section class="characteristics-section">
      <div class="section-heading">
        <Tags size={21} strokeWidth={1.6} />
        <h2>Характеристики</h2>
        <span>{formatNumber(rows().length)}</span>
      </div>
      <Show when={rows().length} fallback={<p class="no-characteristics">В загруженных данных характеристики не указаны.</p>}>
        <dl class="characteristics-table">
          <For each={rows()}>{(attribute) =>
            <div>
              <dt>
                {attribute.name}
                <Show when={classifierNames()}>
                  <span class="create-char-source">{characteristicSourceLabel(sourceFor(attribute))}</span>
                </Show>
              </dt>
              <dd>{attribute.value || "Не указано"}{attribute.unit ? ` ${attribute.unit}` : ""}</dd>
            </div>
          }</For>
        </dl>
      </Show>
    </section>
  );
}

function Gallery(props: { product: Product }) {
  const [selected, setSelected] = createSignal(0);
  return <div class="product-gallery"><div class="gallery-main"><Show when={props.product.imageIds.length} fallback={<ImagePlaceholder />}><ProductImage id={props.product.imageIds[selected()] ?? props.product.imageIds[0]} name={props.product.name} eager /></Show><span class="gallery-caption">СТЕ {props.product.id}</span></div><Show when={props.product.imageIds.length > 1}><div class="gallery-thumbnails" aria-label="Изображения товара"><For each={props.product.imageIds}>{(id, index) => <button classList={{ selected: selected() === index() }} type="button" aria-label={`Изображение ${index() + 1}`} aria-pressed={selected() === index()} onClick={() => setSelected(index())}><ProductImage id={id} name={`${props.product.name}, изображение ${index() + 1}`} /></button>}</For></div></Show></div>;
}

function SimilarProducts(props: { id: number; back: string }) {
  const similar = createAsync(() => getSimilarProducts(props.id));
  return <Show when={similar()}>{items => <Show when={items().length} fallback={<p class="no-characteristics">Похожие товары пока не найдены.</p>}><div class="product-grid"><For each={items()}>{product => <ProductCard product={product} returnTo={props.back} />}</For></div></Show>}</Show>;
}

function Details(props: { id: number; back: string; fromCreate: boolean }) {
  const product = createAsync(() => Number.isSafeInteger(props.id) && props.id > 0 ? getProduct(props.id) : Promise.resolve(null));
  return <Show when={product() !== undefined}><Show when={product()} keyed fallback={<section class="state-panel detail-not-found"><HttpStatusCode code={404} /><Package size={38} strokeWidth={1.3} /><h1>Товар не найден</h1><p>Карточки с таким идентификатором нет в загруженном каталоге.</p><A class={buttonVariants({ variant: "outline" })} href={props.back}>{props.fromCreate ? "К заполнению товара" : "Вернуться к поиску"}<ArrowLeft size={16} /></A></section>}>{item => <>
    <Title>{item.name} — Каталог</Title><Meta name="description" content={`${item.name}. Характеристики и сведения из Портала поставщиков Москвы.`} />
    <nav class="breadcrumbs" aria-label="Хлебные крошки"><A href={props.back}>{props.fromCreate ? "Новый товар" : "Каталог"}</A><Show when={item.category}><ChevronRight size={14} /><A href={`/catalog?category=${item.categoryId}`}>{item.category}</A></Show></nav>
    <section class="product-detail"><Gallery product={item} /><div class="detail-summary"><div class="detail-category">{item.category || "Категория не указана"}</div><h1>{item.name}</h1><p class="detail-manufacturer">{item.manufacturer || "Производитель не указан"}<Show when={item.country}><span class="detail-country"><Globe2 size={14} />{item.country}</span></Show></p>
      <dl class="detail-facts"><div><dt>ID товарной единицы</dt><dd>{item.id}</dd></div><Show when={item.modelName}><div><dt>Модель</dt><dd>{item.modelName}</dd></div></Show><Show when={item.partNumber}><div><dt>Артикул</dt><dd>{item.partNumber}</dd></div></Show><Show when={item.unit}><div><dt>Единица измерения</dt><dd>{item.unit}</dd></div></Show><Show when={item.sourceUpdatedAt}><div><dt>Обновление в источнике</dt><dd>{formatDate(item.sourceUpdatedAt)}</dd></div></Show></dl>
      <a class={`${buttonVariants({ size: "lg" })} original-link`} href={`https://zakupki.mos.ru/sku/view/${item.id}`} target="_blank" rel="noopener noreferrer">Открыть на Портале поставщиков<ArrowUpRight size={18} /></a>
    </div></section>
    <Show when={item.ktruCode || item.okpd2 || item.okpd2Name || item.kpgzCode || item.kpgzName || item.spgzId}>
      <ProductClassification product={item} />
    </Show>
    <div class="detail-sections">
      <Show when={item.description}>
        <section class="description-section">
          <div class="section-heading"><FileText size={20} strokeWidth={1.6} /><h2>Описание</h2></div>
          <p>{item.description}</p>
        </section>
      </Show>
      <ProductCharacteristics product={item} />
    </div>
    <section class="similar-products" aria-labelledby="similar-products-heading"><div class="section-heading"><Sparkles size={21} strokeWidth={1.6} /><h2 id="similar-products-heading">Похожие товары</h2></div><DataBoundary resetKey={String(item.id)} retry={() => revalidate(getSimilarProducts.key)}><Suspense fallback={<ResultsSkeleton />}><SimilarProducts id={item.id} back={props.back} /></Suspense></DataBoundary></section>
  </>}</Show></Show>;
}

function resolveBack(from: string | undefined): { href: string; fromCreate: boolean } {
  if (from === "/" || from?.startsWith("/?")) return { href: "/", fromCreate: true };
  if (from === "/catalog" || from?.startsWith("/catalog?")) return { href: from, fromCreate: false };
  return { href: "/catalog", fromCreate: false };
}

export default function ProductPage() {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const back = () => {
    const from = Array.isArray(searchParams.from) ? searchParams.from[0] : searchParams.from;
    return resolveBack(from);
  };
  return <main id="main-content" class="page-width detail-main"><A href={back().href} class="back-link"><ArrowLeft size={16} />{back().fromCreate ? "К заполнению товара" : "К результатам поиска"}</A><DataBoundary resetKey={params.id} retry={() => revalidate(getProduct.key)}><Suspense fallback={<div class="detail-loading" role="status"><Skeleton class="skeleton-image" /><div><Skeleton class="skeleton-line wide" /><Skeleton class="skeleton-line" /><p>Загружаем карточку товара…</p></div></div>}><Details id={Number(params.id)} back={back().href} fromCreate={back().fromCreate} /></Suspense></DataBoundary></main>;
}
