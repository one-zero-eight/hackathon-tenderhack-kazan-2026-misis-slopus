import { Meta, Title } from "@solidjs/meta";
import { ArrowRight, BookOpen, Check, ChevronDown, ClipboardList, FileText, Info, Lightbulb, PackagePlus, RotateCcw, Sparkles, WandSparkles } from "lucide-solid";
import { batch, createMemo, createSignal, For, Index, Show } from "solid-js";
import { categories, extractCharacteristics, guessCategory, suggestKtruCategory, type KtruSuggestion } from "~/lib/product-draft";
import { Badge, Button, Card, TextField, TextFieldDescription, TextFieldInput, TextFieldLabel, TextFieldTextarea } from "~/ui-kit";
import "./create-product.css";

type Characteristic = { name: string; value: string };
const terms = [
  { code: "КПГЗ", name: "Каталог предметов государственного заказа", description: "Помогает отнести товар к нужной группе закупок." },
  { code: "КТРУ", name: "Каталог товаров, работ и услуг", description: "Содержит типовые позиции и обязательные характеристики для закупок." },
  { code: "ОКПД 2", name: "Классификатор продукции", description: "Код вида продукции по экономической деятельности." },
  { code: "ОКЕИ", name: "Классификатор единиц измерения", description: "Определяет единицу товара: штука, упаковка, килограмм и другие." },
];
const questions = [
  { question: "Как правильно назвать товар?", answer: "Начните с типа товара, затем добавьте отличительные признаки: материал, размер, назначение или объём. Избегайте рекламных слов и характеристик, которых нет в описании." },
  { question: "Что писать в артикуле?", answer: "Укажите ваш внутренний код товара. Он должен отличаться от артикулов других товаров и помогать вам быстро находить нужную позицию." },
  { question: "Можно ли изменить категорию?", answer: "Да. После нажатия «Заполнить» мы предлагаем позицию КТРУ по описанию. Вы можете выбрать другую категорию из списка. Подбор работает на небольшом демонстрационном наборе позиций." },
  { question: "Откуда берутся характеристики?", answer: "В этой версии мы ищем явные значения в описании по шаблонам выбранной категории. Проверьте их и при необходимости исправьте вручную. Данные никуда не отправляются." },
];

export default function NewProduct() {
  const [name, setName] = createSignal("");
  const [sku, setSku] = createSignal("");
  const [model, setModel] = createSignal("");
  const [brand, setBrand] = createSignal("");
  const [description, setDescription] = createSignal("");
  const [manualCategory, setManualCategory] = createSignal<string | null>(null);
  const [recommended, setRecommended] = createSignal<KtruSuggestion | null>(null);
  const [fallbackCategory, setFallbackCategory] = createSignal("");
  const [attempted, setAttempted] = createSignal(false);
  const [characteristics, setCharacteristics] = createSignal<Characteristic[]>([]);
  const [generatedFor, setGeneratedFor] = createSignal("");
  const [preview, setPreview] = createSignal(false);
  const categoryId = createMemo(() => manualCategory() ?? recommended()?.categoryId ?? fallbackCategory());
  const category = createMemo(() => categories.find(item => item.id === categoryId()));
  const fingerprint = () => `${categoryId()}\u0000${description()}`;
  const outdated = () => !!generatedFor() && generatedFor() !== fingerprint();
  const filledCount = () => characteristics().filter(item => item.value.trim()).length;

  const changeCategory = (id: string | null) => {
    batch(() => {
      setManualCategory(id);
      setCharacteristics([]);
      setGeneratedFor("");
      setPreview(false);
    });
  };

  const fillExample = () => {
    setName("Бумага офисная А4, 80 г/м², 500 листов");
    setSku("BUM-A4-500");
    setModel("");
    setBrand("");
    setManualCategory(null);
    setRecommended(null);
    setFallbackCategory("");
    setAttempted(false);
    setDescription("Бумага офисная формата А4, плотность 80 г/м², белая, в упаковке 500 листов. Подходит для печати документов на лазерном и струйном принтере.");
    setCharacteristics([]);
    setGeneratedFor("");
    setPreview(false);
  };
  const reset = () => {
    setName(""); setSku(""); setModel(""); setBrand(""); setDescription("");
    setManualCategory(null); setRecommended(null); setFallbackCategory(""); setAttempted(false);
    setCharacteristics([]); setGeneratedFor(""); setPreview(false);
  };
  const generate = () => {
    if (!description().trim()) return;
    const suggestion = suggestKtruCategory(description());
    const fallback = guessCategory(description());
    const selected = manualCategory() ?? suggestion?.categoryId ?? fallback;
    batch(() => {
      setRecommended(suggestion);
      setFallbackCategory(fallback);
      setAttempted(true);
      setCharacteristics(extractCharacteristics(selected, description()));
      setGeneratedFor(`${selected}\u0000${description()}`);
      setPreview(false);
    });
  };
  const updateCharacteristic = (name: string, value: string) => setCharacteristics(items => items.map(item => item.name === name ? { ...item, value } : item));
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (!name().trim() || !sku().trim() || !categoryId()) return;
    setPreview(true);
    requestAnimationFrame(() => document.getElementById("draft-preview")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  return <main id="main-content" class="page-width create-main">
    <Title>Создать товар — Каталог</Title>
    <Meta name="description" content="Создайте карточку товара для закупок. Демонстрационная форма с подсказками по категориям и характеристикам." />
    <div class="create-heading">
      <div><div class="create-eyebrow"><span class="status-dot" /> НОВАЯ КАРТОЧКА <span class="eyebrow-divider">/</span> ДЕМО-РЕЖИМ</div>
        <h1>Создать <em>товар</em><span class="create-asterisk" aria-hidden="true"><Sparkles strokeWidth={1.2} /></span></h1>
        <p>Всё нужное для карточки — в одном месте. Начните с описания,<br class="desktop-break" /> а мы подскажем категорию КТРУ и заполним характеристики.</p>
      </div>
      <div class="create-heading-actions"><Button variant="outline" size="sm" type="button" onClick={fillExample}><Lightbulb size={15} />Заполнить пример</Button><Button variant="ghost" size="sm" type="button" onClick={reset}><RotateCcw size={14} />Очистить</Button></div>
    </div>
    <div class="demo-banner" role="note"><Info size={17} /><span><strong>Это макет интерфейса.</strong> Данные остаются на этой странице, товар не создаётся на Портале поставщиков.</span></div>
    <div class="create-layout">
      <form class="create-form" onSubmit={submit}>
        <Card class="create-section create-description"><div class="create-section-top"><div class="step-number">01</div><div><h2>Описание товара</h2><p>Расскажите о товаре — предложим категорию КТРУ и заполним характеристики.</p></div></div>
          <div class="create-section-body">
            <TextField class="create-field" name="product-description" value={description()} onChange={setDescription}><TextFieldLabel>Описание</TextFieldLabel><TextFieldTextarea id="product-description" rows={6} placeholder="Опишите назначение, материал, размеры, комплектацию и другие особенности товара…" maxlength={3000} /><TextFieldDescription>Пишите только о том, что можете подтвердить. Чем точнее описание, тем полезнее подсказки.</TextFieldDescription></TextField>
            <div class="description-bottom"><span>Без рекламных обещаний и неподтверждённых свойств</span><span>{description().length} / 3000</span></div>
            <div class="autofill-panel"><div class="autofill-icon"><WandSparkles size={20} /></div><div class="autofill-copy"><strong>Заполнение по описанию</strong><span>Подберём категорию КТРУ и перенесём найденные значения в характеристики.</span></div><Button type="button" variant="secondary" size="sm" disabled={!description().trim()} onClick={generate}><Sparkles size={15} />Заполнить</Button></div>
            <Show when={!description().trim()}><p class="autofill-hint">Добавьте описание товара, чтобы предложить категорию и заполнить характеристики.</p></Show>
            <div class="autofill-result" role="status" aria-live="polite">
              <Show when={attempted()}>
                <Show when={recommended()} fallback={<p class="autofill-hint">В демо-наборе не найдена подходящая категория КТРУ. Уточните описание или выберите категорию вручную.</p>}>{suggestion => <div class="ktru-suggestion"><div class="ktru-suggestion-heading"><Check size={16} /><strong>Предложенная категория КТРУ</strong><Badge variant="secondary">По описанию</Badge></div><p class="ktru-suggestion-name">{suggestion().name}</p><p class="ktru-suggestion-code">Код КТРУ: <span>{suggestion().code}</span></p></div>}</Show>
                <Show when={outdated()}><p class="autofill-hint">Описание или категория изменились. Нажмите «Заполнить» ещё раз, чтобы обновить результат.</p></Show>
                <p class="autofill-hint">Демо-подбор по нескольким позициям КТРУ. Проверьте предложенную категорию и значения характеристик.</p>
              </Show>
            </div>
          </div>
        </Card>
        <Card class="create-section"><div class="create-section-top"><div class="step-number">02</div><div><h2>Основная информация</h2><p>Укажите название и реквизиты товара, проверьте категорию.</p></div></div>
          <div class="create-section-body">
            <TextField class="create-field" name="product-name" value={name()} onChange={setName} required><TextFieldLabel>Наименование <span class="required-mark">*</span></TextFieldLabel><TextFieldInput id="product-name" required placeholder="Например, бумага офисная А4, 80 г/м²" maxlength={200} /><TextFieldDescription>Укажите вид товара и его основные отличительные признаки.</TextFieldDescription></TextField>
            <div class="create-category"><div class="category-heading"><div class="category-icon"><Sparkles size={18} /></div><div><strong>Категория товара</strong><span>Заполняем по описанию после нажатия «Заполнить» — выбор всегда за вами</span></div><Show when={recommended() && manualCategory() === null}><Badge variant="secondary" class="category-auto-badge">По описанию</Badge></Show></div>
              <Show when={recommended() && manualCategory() !== null && manualCategory() !== recommended()?.categoryId}><button class="category-reset" type="button" onClick={() => changeCategory(null)}>Вернуть предложенную категорию: {recommended()?.name}</button></Show>
              <label for="product-category" class="select-label">Категория <span class="required-mark">*</span></label><div class="create-select-wrap"><select id="product-category" name="product-category" required value={categoryId()} onChange={event => changeCategory(event.currentTarget.value)}><option value="">Выберите категорию</option><For each={categories}>{item => <option value={item.id}>{item.name}</option>}</For></select><ChevronDown size={17} aria-hidden="true" /></div>
              <Show when={category()}><p class="category-path">{category()?.path}</p></Show>
            </div>
            <div class="create-fields-row"><TextField class="create-field" name="product-sku" value={sku()} onChange={setSku} required><TextFieldLabel>Артикул <span class="required-mark">*</span></TextFieldLabel><TextFieldInput id="product-sku" required placeholder="Ваш код товара" maxlength={80} /><TextFieldDescription>Уникальный код в вашем учёте</TextFieldDescription></TextField><TextField class="create-field" name="product-model" value={model()} onChange={setModel}><TextFieldLabel>Модель <span class="optional-mark">необязательно</span></TextFieldLabel><TextFieldInput id="product-model" placeholder="Например, Premium A4" maxlength={120} /></TextField></div>
            <TextField class="create-field" name="product-brand" value={brand()} onChange={setBrand}><TextFieldLabel>Бренд <span class="optional-mark">необязательно</span></TextFieldLabel><TextFieldInput id="product-brand" placeholder="Укажите, если есть" maxlength={120} /></TextField>
          </div>
        </Card>
        <Card class="create-section"><div class="create-section-top"><div class="step-number">03</div><div><h2>Характеристики</h2><p>Подставим значения из описания для выбранной категории.</p></div></div>
          <div class="create-section-body">
            <Show when={!characteristics().length}><p class="autofill-hint">Добавьте описание в начале страницы и нажмите «Заполнить», чтобы получить поля характеристик.</p></Show>
            <Show when={characteristics().length}><div class="characteristic-heading"><span>{outdated() ? "Описание изменилось — заполните заново, чтобы обновить подсказки" : `Найдено значений: ${filledCount()} из ${characteristics().length}`}</span><Badge variant="outline">Можно исправить</Badge></div><div class="characteristic-fields"><Index each={characteristics()}>{item => <TextField class="create-field" value={item().value} onChange={value => updateCharacteristic(item().name, value)}><TextFieldLabel>{item().name}</TextFieldLabel><TextFieldInput placeholder="Укажите значение" /></TextField>}</Index></div><Show when={filledCount() === 0}><p class="autofill-hint">Явных значений не нашлось. Заполните поля вручную или дополните описание.</p></Show></Show>
          </div>
        </Card>
        <div class="create-form-footer"><div class="footer-note"><Info size={15} />Предпросмотр не сохраняет и не отправляет данные.</div><Button type="submit" size="lg">Посмотреть карточку <ArrowRight size={17} /></Button></div>
        <Show when={preview()}><Card id="draft-preview" class="preview-card" aria-live="polite"><div class="preview-top"><div><span class="preview-eyebrow">ПРЕДПРОСМОТР · НЕ СОХРАНЕНО</span><h2>{name().trim()}</h2></div><Badge variant="secondary">Черновик</Badge></div><p class="preview-category">{category()?.path || "Категория не выбрана"}</p><dl class="preview-details"><div><dt>Артикул</dt><dd>{sku().trim()}</dd></div><Show when={model().trim()}><div><dt>Модель</dt><dd>{model().trim()}</dd></div></Show><Show when={brand().trim()}><div><dt>Бренд</dt><dd>{brand().trim()}</dd></div></Show><For each={characteristics().filter(item => item.value.trim())}>{item => <div><dt>{item.name}</dt><dd>{item.value.trim()}</dd></div>}</For></dl><Show when={description().trim()}><p class="preview-description">{description().trim()}</p></Show><p class="preview-disclaimer">Только локальный предпросмотр. Эта карточка не опубликована.</p></Card></Show>
      </form>
      <aside class="create-aside" aria-label="Подсказки по заполнению"><Card class="help-card help-intro"><div class="help-icon"><BookOpen size={20} /></div><h2>Поможем разобраться</h2><p>Короткие подсказки о терминах и заполнении карточки — всегда под рукой.</p></Card>
        <Card class="help-card"><div class="help-card-title"><ClipboardList size={19} /><h2>Словарь поставщика</h2></div><p class="help-card-subtitle">Необязательно знать всё наизусть</p><div class="term-list"><For each={terms}>{term => <details class="term-item"><summary><span class="term-code">{term.code}</span><span class="term-name">{term.name}</span><ChevronDown size={16} /></summary><p>{term.description}</p></details>}</For></div><div class="help-footnote"><Info size={14} />Коды классификаторов в демоверсии не назначаются автоматически.</div></Card>
        <Card class="help-card"><div class="help-card-title"><FileText size={19} /><h2>Вопросы и ответы</h2></div><div class="faq-list"><For each={questions}>{item => <details class="faq-item"><summary>{item.question}<ChevronDown size={16} /></summary><p>{item.answer}</p></details>}</For></div></Card>
        <div class="help-bottom"><Sparkles size={16} /><span>От первого поля до готовой карточки — шаг за шагом.</span></div>
      </aside>
    </div>
  </main>;
}
