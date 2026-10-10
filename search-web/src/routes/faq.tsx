import { Meta, Title } from "@solidjs/meta";
import ChevronDown from "lucide-solid/icons/chevron-down";
import ClipboardList from "lucide-solid/icons/clipboard-list";
import ExternalLink from "lucide-solid/icons/external-link";
import FileText from "lucide-solid/icons/file-text";
import { For } from "solid-js";
import "./faq.css";

const terms = [
  {
    code: "КПГЗ и СПГЗ",
    name: "Классификатор и справочник предметов госзаказа Москвы",
    description: "КПГЗ — иерархия категорий товаров, работ и услуг для нужд Москвы. СПГЗ — стандартные позиции с характеристиками внутри этих категорий.",
    sources: [
      { label: "Определение в регламенте · PDF, с. 3", url: "https://zakupki.mos.ru/cms/Media/docs/%D0%A0%D0%B5%D0%B3%D0%BB%D0%B0%D0%BC%D0%B5%D0%BD%D1%82_%D0%B8%D0%BD%D1%84%D0%BE%D1%80%D0%BC%D0%B0%D1%86%D0%B8%D0%BE%D0%BD%D0%BD%D0%BE%D0%B3%D0%BE_%D0%B2%D0%B7%D0%B0%D0%B8%D0%BC%D0%BE%D0%B4%D0%B5%D0%B9%D1%81%D1%82%D0%B2%D0%B8%D1%8F.pdf#page=3" },
    ],
  },
  {
    code: "ОКПД 2 и КТРУ",
    name: "Общероссийский классификатор и каталог товаров, работ, услуг",
    description: "ОКПД 2 кодирует продукцию по видам экономической деятельности. КТРУ — позиции закупок с наименованиями, единицами измерения и характеристиками, привязанные к кодам ОКПД 2.",
    sources: [
      { label: "Карточка Росстандарта · ОК 034-2014", url: "https://protect.gost.ru/classificators/details/d62a528e-5761-40c2-a67b-2966a9d0911c" },
      { label: "Каталог КТРУ в ЕИС", url: "https://zakupki.gov.ru/epz/ktru/start/startPage.html" },
    ],
  },
  {
    code: "ОКЕИ",
    name: "Общероссийский классификатор единиц измерения",
    description: "Содержит коды и обозначения единиц измерения, например штуки и килограмма.",
    sources: [
      { label: "Карточка Росстандарта · ОК 015-94", url: "https://protect.gost.ru/classificators/details/08242108-d2a4-4343-b89a-5d412e563643" },
    ],
  },
];

const questions = [
  { question: "Как правильно назвать товар?", answer: "Начните с типа товара, затем добавьте отличительные признаки: материал, размер, назначение или объём. Избегайте рекламных слов и характеристик, которых нет в описании." },
  { question: "Что писать в артикуле?", answer: "Укажите ваш внутренний код товара. Он должен отличаться от артикулов других товаров и помогать вам быстро находить нужную позицию." },
  { question: "Можно ли изменить категорию?", answer: "Да. Откройте выбор категории КТРУ и найдите нужную позицию по дереву или быстрому поиску. Подтверждается только позиция КТРУ, не группа ОКПД2." },
  { question: "Откуда берутся характеристики?", answer: "Имена полей берутся из выбранной позиции КТРУ. Значения при необходимости заполните вручную. Данные никуда не отправляются." },
];

export default function FaqPage() {
  return <main id="main-content" class="page-width faq-main">
    <Title>FAQ — Каталог</Title>
    <Meta name="description" content="Классификаторы, каталог и ответы на частые вопросы при создании карточки товара." />

    <section class="faq-block" aria-labelledby="classifiers-heading">
      <div class="section-heading"><ClipboardList size={20} strokeWidth={1.6} /><h1 id="classifiers-heading">Классификаторы и каталог</h1></div>
      <p class="faq-lead">Официальные источники и обозначения, которые встречаются в карточке.</p>
      <div class="term-list"><For each={terms}>{term => <details class="term-item">
        <summary><span class="term-code">{term.code}</span><span class="term-name">{term.name}</span><ChevronDown size={16} /></summary>
        <div class="term-definition">
          <p>{term.description}</p>
          <div class="term-sources"><For each={term.sources}>{source =>
            <a class="term-source" href={source.url} target="_blank" rel="noopener noreferrer">{source.label}<ExternalLink size={13} aria-hidden="true" /></a>
          }</For></div>
        </div>
      </details>}</For></div>
    </section>

    <section class="faq-block" aria-labelledby="faq-heading">
      <div class="section-heading"><FileText size={20} strokeWidth={1.6} /><h2 id="faq-heading">Вопросы и ответы</h2></div>
      <div class="faq-list"><For each={questions}>{item => <details class="faq-item"><summary>{item.question}<ChevronDown size={16} /></summary><p>{item.answer}</p></details>}</For></div>
    </section>
  </main>;
}
