import { Title } from "@solidjs/meta";
import { A } from "@solidjs/router";
import { HttpStatusCode } from "@solidjs/start";
import ArrowRight from "lucide-solid/icons/arrow-right";
import Search from "lucide-solid/icons/search";
import { buttonVariants } from "~/ui-kit";

export default function NotFound() {
  return <main id="main-content" class="page-width not-found"><Title>Страница не найдена — Каталог</Title><HttpStatusCode code={404} /><span class="error-code">404</span><div class="state-icon"><Search size={30} /></div><h1>Здесь пока ничего нет.</h1><p>Возможно, ссылка изменилась.<br />Но нужный товар ещё можно найти в каталоге.</p><A href="/" class={buttonVariants({ size: "lg" })}>Перейти к поиску<ArrowRight size={17} /></A></main>;
}
