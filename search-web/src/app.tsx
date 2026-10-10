import { MetaProvider, Title } from "@solidjs/meta";
import { A, Router } from "@solidjs/router";
import { FileRoutes } from "@solidjs/start/router";
import ArrowUpRight from "lucide-solid/icons/arrow-up-right";
import BookOpen from "lucide-solid/icons/book-open";
import CircleHelp from "lucide-solid/icons/circle-help";
import Plus from "lucide-solid/icons/plus";
import { Suspense } from "solid-js";
import "./app.css";

export default function App() {
  return <Router root={props => <MetaProvider>
    <Title>Каталог — товары для закупок</Title>
    <a class="skip-link" href="#main-content">К содержимому</a>
    <header class="site-header"><div class="page-width header-inner">
      <nav aria-label="Основная навигация">
        <A href="/" end activeClass="nav-active"><Plus size={15} />Новый товар</A>
        <A href="/catalog" activeClass="nav-active">Каталог товаров</A>
        <A href="/directories" activeClass="nav-active"><BookOpen size={15} />Справочники</A>
        <A href="/faq" activeClass="nav-active"><CircleHelp size={15} />FAQ</A>
      </nav>
      <a class="source-link" href="https://zakupki.mos.ru/" target="_blank" rel="noopener noreferrer">Портал поставщиков<ArrowUpRight size={16} /></a>
    </div></header>
    <Suspense fallback={<main id="main-content" class="page-width route-loading" role="status" aria-live="polite">Загружаем…</main>}>{props.children}</Suspense>
  </MetaProvider>}><FileRoutes /></Router>;
}
