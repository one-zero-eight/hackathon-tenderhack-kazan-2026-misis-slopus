import { Meta, Title } from "@solidjs/meta";
import { createAsync, revalidate, useIsRouting, useLocation, useSearchParams } from "@solidjs/router";
import { ArrowDown, ArrowLeft, ArrowRight, Check, ChevronDown, Layers3, Search, SlidersHorizontal, Sparkles, TextSearch } from "lucide-solid";
import { createEffect, createMemo, createSignal, For, onCleanup, onMount, Show, Suspense } from "solid-js";
import { DataBoundary, EmptyState, ProductCard, ResultsSkeleton } from "~/components/catalog";
import { Badge, Button, TextField, TextFieldInput, TextFieldLabel } from "~/ui-kit";
import { getCatalogStats, searchProducts } from "~/lib/rpc";
import { RETRIEVAL_LIMIT } from "~/lib/search-config";
import type { SearchInput, SearchMode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";

const modes: { value: SearchMode; label: string; description: string; icon: typeof Layers3 }[] = [
  { value: "hybrid", label: "Гибридный", description: "Смысл и точные слова", icon: Layers3 },
  { value: "semantic", label: "По смыслу", description: "Находит близкие по смыслу товары", icon: Sparkles },
  { value: "text", label: "По словам", description: "Ищет совпадения в тексте", icon: TextSearch },
];
const param = (value: string | string[] | undefined) => Array.isArray(value) ? value[0] || "" : value || "";
const positiveInt = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : undefined;

function CatalogNumbers() {
  const stats = createAsync(() => getCatalogStats());
  return <Show when={stats()}>{data => <>
    <div class="catalog-numbers"><div><strong>{formatNumber(data().products)}</strong><span>товаров в каталоге</span></div><div><strong>{formatNumber(data().categories.length)}</strong><span>категорий</span></div><div><strong class="index-number"><Sparkles size={19} />{formatNumber(data().embedded)}</strong><span>товаров в смысловом индексе</span></div></div>
  </>}</Show>;
}

function CategorySelect(props: { value: string; onChange: (value: string) => void }) {
  const stats = createAsync(() => getCatalogStats());
  return <div class="category-select"><SlidersHorizontal size={16} /><select aria-label="Категория товаров" value={props.value} onChange={event => props.onChange(event.currentTarget.value)}><option value="">Все категории</option><For each={stats()?.categories}>{category => <option value={category.id}>{category.name} ({formatNumber(category.count)})</option>}</For></select><ChevronDown size={15} /></div>;
}

function Results(props: { input: SearchInput; returnTo: string; reset: () => void; setPage: (page: number) => void }) {
  const result = createAsync(() => searchProducts(props.input));
  return <Show when={result()}>{data => <>
    <div class="results-heading"><div><h2>{props.input.query ? "Результаты поиска" : "Товары в каталоге"}<span>{formatNumber(data().total)}</span></h2><p>{props.input.query ? <>По запросу «{props.input.query}» · {modes.find(mode => mode.value === data().mode)?.label.toLowerCase()} поиск</> : "Выбирайте из реальных товаров или начните с поиска"}</p><Show when={props.input.query && data().mode !== "text" && data().total >= RETRIEVAL_LIMIT}><p>Показаны {RETRIEVAL_LIMIT} лучших совпадений, не общее число подходящих товаров.</p></Show></div><span class="search-timing">{data().durationMs < 1000 ? `${Math.round(data().durationMs)} мс` : `${(data().durationMs / 1000).toFixed(2)} с`}</span></div>
    <Show when={data().items.length} fallback={<EmptyState query={props.input.query} reset={props.reset} />}><div class="product-grid"><For each={data().items}>{product => <ProductCard product={product} returnTo={props.returnTo} />}</For></div></Show>
    <Show when={data().page > 1 || data().hasMore}><nav class="pagination" aria-label="Страницы результатов"><Button variant="outline" disabled={data().page <= 1} onClick={() => props.setPage(data().page - 1)}><ArrowLeft />Назад</Button><span>Страница {data().page}</span><Button variant="outline" disabled={!data().hasMore} onClick={() => props.setPage(data().page + 1)}>Далее<ArrowRight /></Button></nav></Show>
  </>}</Show>;
}

export default function Home() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const isRouting = useIsRouting();
  onMount(() => {
    const timer = setInterval(() => { void revalidate(getCatalogStats.key); }, 15000);
    onCleanup(() => clearInterval(timer));
  });
  const query = () => param(params.q).trim();
  const mode = (): SearchMode => ["hybrid", "semantic", "text"].includes(param(params.mode)) ? param(params.mode) as SearchMode : "hybrid";
  const category = () => positiveInt(param(params.category));
  const [draft, setDraft] = createSignal(query());
  createEffect(() => setDraft(query()));
  const input = createMemo<SearchInput>(() => ({ query: query(), mode: mode(), categoryId: category(), page: positiveInt(param(params.page)) ?? 1 }));
  const returnTo = () => location.pathname + location.search;
  const reset = () => setParams({ q: undefined, category: undefined, page: undefined });
  const submit = (event: SubmitEvent) => { event.preventDefault(); setParams({ q: draft().trim() || undefined, page: undefined }); };
  let searchInput!: HTMLInputElement;
  const returnToSearch = (event: MouseEvent) => {
    event.preventDefault();
    searchInput.focus({ preventScroll: true });
    searchInput.scrollIntoView({ behavior: "instant", block: "center" });
  };
  return <main id="main-content" class="page-width catalog-main">
    <Title>{query() ? `${query()} — Каталог` : "Каталог — А ещё мы сделали поиск"}</Title>
    <Meta name="description" content="Ищите товары для закупок по смыслу и точным словам. Реальные данные Портала поставщиков Москвы, характеристики и ссылки на первоисточник." />
    <section class="hero"><h1>А ещё мы<br />сделали <em>поиск</em><span class="hero-asterisk" aria-hidden="true"><Sparkles strokeWidth={1.1} /></span></h1>
      <form class="search-form" role="search" onSubmit={submit}>
        <Search class="search-form-icon" size={23} strokeWidth={1.7} />
        <TextField class="search-text-field" name="q" value={draft()} onChange={setDraft}><TextFieldLabel class="sr-only">Что вы ищете?</TextFieldLabel><TextFieldInput ref={searchInput} id="catalog-search" type="search" class="search-input" placeholder="Название товара, характеристика или задача…" autocomplete="off" /></TextField>
        <Button type="submit" size="lg" class="search-submit"><span>Найти</span><ArrowRight size={18} /></Button>
      </form>
      <DataBoundary retry={() => revalidate(getCatalogStats.key)}><Suspense fallback={<div class="stats-loading" role="status">Загружаем сведения о каталоге…</div>}><CatalogNumbers /></Suspense></DataBoundary>
    </section>
    <section class="catalog-section" id="results" aria-label="Каталог товаров" aria-busy={isRouting()}>
      <div class="catalog-toolbar"><fieldset class="search-modes"><legend class="sr-only">Режим поиска</legend><For each={modes}>{option => <label classList={{ selected: mode() === option.value }} title={option.description}><input type="radio" name="mode" value={option.value} checked={mode() === option.value} onChange={() => setParams({ mode: option.value, page: undefined })} /><option.icon size={16} /><span>{option.label}</span><Show when={mode() === option.value}><Check size={13} class="mode-check" /></Show></label>}</For></fieldset>
        <DataBoundary retry={() => revalidate(getCatalogStats.key)}><Suspense fallback={<span class="filter-loading">Загружаем категории…</span>}><CategorySelect value={category() ? String(category()) : ""} onChange={value => setParams({ category: value || undefined, page: undefined })} /></Suspense></DataBoundary>
      </div>
      <div class="mode-explanation"><Sparkles size={14} /><span>{mode() === "hybrid" ? "Два способа поиска вместе: точные совпадения и близость по смыслу." : mode() === "semantic" ? "Опишите назначение или свойства — поиск найдёт близкие по смыслу товары." : "Поиск по словам в названии, описании и характеристиках."}</span><Show when={isRouting()}><Badge variant="secondary" class="pending-badge">Обновляем…</Badge></Show></div>
      <DataBoundary resetKey={JSON.stringify(input())} retry={() => revalidate(searchProducts.key)}><Suspense fallback={<ResultsSkeleton />}><Results input={input()} returnTo={returnTo()} reset={reset} setPage={page => { setParams({ page: page === 1 ? undefined : page }); document.getElementById("results")?.scrollIntoView({ behavior: "smooth", block: "start" }); }} /></Suspense></DataBoundary>
    </section>
    <a class="back-to-search" href="#catalog-search" onClick={returnToSearch}>К поиску<ArrowDown size={15} /></a>
  </main>;
}
