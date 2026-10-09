import { MetaProvider, Title } from "@solidjs/meta";
import { A, Router } from "@solidjs/router";
import { FileRoutes } from "@solidjs/start/router";
import { ArrowUpRight, Boxes, Plus } from "lucide-solid";
import { Suspense } from "solid-js";
import "./app.css";

export default function App() {
  return <Router root={props => <MetaProvider>
    <Title>Каталог — товары для закупок</Title>
    <a class="skip-link" href="#main-content">К содержимому</a>
    <header class="site-header"><div class="page-width header-inner">
      <A href="/" class="brand" aria-label="Каталог — главная"><span class="brand-symbol"><Boxes size={23} strokeWidth={1.7} /></span><span>каталог<span class="brand-period">.</span></span></A>
      <nav aria-label="Основная навигация"><A href="/" end activeClass="nav-active"><Plus size={15} />Создать товар</A><A href="/catalog" activeClass="nav-active">Каталог товаров</A></nav>
      <a class="source-link" href="https://zakupki.mos.ru/" target="_blank" rel="noopener noreferrer">Портал поставщиков<ArrowUpRight size={16} /></a>
    </div></header>
    <Suspense fallback={<main id="main-content" class="page-width route-loading" role="status">Загружаем страницу…</main>}>{props.children}</Suspense>
  </MetaProvider>}><FileRoutes /></Router>;
}
