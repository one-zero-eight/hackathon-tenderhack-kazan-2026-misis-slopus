import { Meta, Title } from "@solidjs/meta";
import { ArrowRight, Check, ChevronDown, ClipboardList, FileText, Info, Lightbulb, RotateCcw, Sparkles, WandSparkles } from "lucide-solid";
import { batch, createMemo, createSignal, For, Index, Show } from "solid-js";
import { characteristicsFromKtruRecord, KtruPickerActions, KtruPickerBody } from "~/components/ktru-picker";
import { getKtruRaw, searchKtru } from "~/lib/rpc";
import type { KtruTreeNode } from "~/lib/types";
import { Badge, Button, Card, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, TextField, TextFieldDescription, TextFieldInput, TextFieldLabel, TextFieldTextarea } from "~/ui-kit";
import "./create-product.css";
import "./ktru.css";

type Characteristic = { name: string; value: string };
const questions = [
  { question: "Что такое КПГЗ?", answer: "Каталог предметов государственного заказа. Помогает отнести товар к нужной группе закупок." },
  { question: "Что такое КТРУ?", answer: "Каталог товаров, работ и услуг. Содержит типовые позиции и обязательные характеристики для закупок. Категория на этой странице выбирается из полного каталога КТРУ." },
  { question: "Что такое ОКПД 2?", answer: "Классификатор продукции — код вида продукции по экономической деятельности. В дереве КТРУ группы ОКПД2 помогают найти нужную позицию." },
  { question: "Что такое ОКЕИ?", answer: "Классификатор единиц измерения. Определяет единицу товара: штука, упаковка, килограмм и другие." },
  { question: "Как правильно назвать товар?", answer: "Начните с типа товара, затем добавьте отличительные признаки: материал, размер, назначение или объём. Избегайте рекламных слов и характеристик, которых нет в описании." },
  { question: "Что писать в артикуле?", answer: "Укажите ваш внутренний код товара. Он должен отличаться от артикулов других товаров и помогать вам быстро находить нужную позицию." },
  { question: "Можно ли изменить категорию?", answer: "Да. Откройте выбор категории КТРУ и найдите нужную позицию по дереву или быстрому поиску. Подтверждается только позиция КТРУ, не группа ОКПД2." },
  { question: "Откуда берутся характеристики?", answer: "Имена полей берутся из выбранной позиции КТРУ. Значения при необходимости заполните вручную. Данные никуда не отправляются." },
];

function guessValue(name: string, description: string): string {
  const text = description.toLocaleLowerCase("ru");
  const n = name.toLocaleLowerCase("ru");
  if (/формат/.test(n)) return (text.match(/\b(а[345])\b/i)?.[1] ?? "").toUpperCase();
  if (/плотност/.test(n)) {
    const value = text.match(/(\d{2,3}(?:[.,]\d+)?)\s*(?:г\s*\/\s*м\s*(?:²|2)|г\/м)/i)?.[1];
    return value ? `${value} г/м²` : "";
  }
  if (/лист/.test(n)) {
    const value = text.match(/(\d{1,4})\s*лист/i)?.[1];
    return value ? `${value} шт.` : "";
  }
  if (/цвет/.test(n)) {
    if (/бел/.test(text)) return "Белый";
    if (/ч[её]рн/.test(text)) return "Чёрный";
  }
  if (/материал/.test(n)) return text.match(/(металл(?:ический)?|пластик(?:овый)?|дерев(?:о|янный)|ткань|кожа)/i)?.[1] ?? "";
  return "";
}

export default function NewProduct() {
  const [name, setName] = createSignal("");
  const [sku, setSku] = createSignal("");
  const [model, setModel] = createSignal("");
  const [brand, setBrand] = createSignal("");
  const [description, setDescription] = createSignal("");
  const [selected, setSelected] = createSignal<KtruTreeNode | null>(null);
  const [pickerSelected, setPickerSelected] = createSignal<KtruTreeNode | null>(null);
  const [pickerOpen, setPickerOpen] = createSignal(false);
  const [pickerFilter, setPickerFilter] = createSignal("");
  const [attempted, setAttempted] = createSignal(false);
  const [characteristics, setCharacteristics] = createSignal<Characteristic[]>([]);
  const [generatedFor, setGeneratedFor] = createSignal("");
  const [preview, setPreview] = createSignal(false);
  const fingerprint = () => `${selected()?.code ?? ""}\u0000${description()}`;
  const outdated = () => !!generatedFor() && generatedFor() !== fingerprint();
  const filledCount = () => characteristics().filter((item) => item.value.trim()).length;
  const categoryPath = createMemo(() => {
    const node = selected();
    if (!node) return "";
    return `КТРУ › ${node.code} › ${node.name}`;
  });

  const applyKtru = async (node: KtruTreeNode, fillValues: boolean) => {
    const record = await getKtruRaw(node.code);
    const fields = characteristicsFromKtruRecord(record).map((item) => ({
      name: item.name,
      value: fillValues ? guessValue(item.name, description()) : "",
    }));
    batch(() => {
      setSelected(node);
      setCharacteristics(fields);
      setGeneratedFor(`${node.code}\u0000${description()}`);
      setPreview(false);
    });
  };

  const openPicker = (filter = "") => {
    setPickerFilter(filter);
    setPickerSelected(selected());
    setPickerOpen(true);
  };

  const confirmPicker = (node: KtruTreeNode) => {
    setPickerOpen(false);
    void applyKtru(node, true);
  };

  const fillExample = () => {
    setName("Бумага офисная А4, 80 г/м², 500 листов");
    setSku("BUM-A4-500");
    setModel("");
    setBrand("");
    setSelected(null);
    setAttempted(false);
    setDescription("Бумага офисная формата А4, плотность 80 г/м², белая, в упаковке 500 листов. Подходит для печати документов на лазерном и струйном принтере.");
    setCharacteristics([]);
    setGeneratedFor("");
    setPreview(false);
  };

  const reset = () => {
    setName(""); setSku(""); setModel(""); setBrand(""); setDescription("");
    setSelected(null); setAttempted(false);
    setCharacteristics([]); setGeneratedFor(""); setPreview(false);
  };

  const generate = async () => {
    if (!description().trim()) return;
    setAttempted(true);
    const hits = await searchKtru(description());
    const best = hits.find((item) => item.status === "ACTIVE") ?? hits[0];
    if (!best) {
      batch(() => {
        setSelected(null);
        setCharacteristics([]);
        setGeneratedFor(`\u0000${description()}`);
        setPreview(false);
      });
      openPicker(description().trim().slice(0, 80));
      return;
    }
    await applyKtru(best, true);
  };

  const updateCharacteristic = (name: string, value: string) => setCharacteristics((items) => items.map((item) => item.name === name ? { ...item, value } : item));
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (!name().trim() || !sku().trim() || !selected()) return;
    setPreview(true);
    requestAnimationFrame(() => document.getElementById("draft-preview")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  return <main id="main-content" class="page-width create-main">
    <Title>Создать товар — Каталог</Title>
    <Meta name="description" content="Создайте карточку товара для закупок. Выбор категории КТРУ с иерархией и быстрым поиском." />
    <div class="create-heading">
      <div>
        <h1>Создать <em>товар</em><span class="create-asterisk" aria-hidden="true"><Sparkles strokeWidth={1.2} /></span></h1>
        <p>Всё нужное для карточки — в одном месте. Начните с описания,<br class="desktop-break" /> а мы подскажем категорию КТРУ и заполним характеристики.</p>
      </div>
      <div class="create-heading-actions"><Button variant="outline" size="sm" type="button" onClick={fillExample}><Lightbulb size={15} />Заполнить пример</Button><Button variant="ghost" size="sm" type="button" onClick={reset}><RotateCcw size={14} />Очистить</Button></div>
    </div>
    <div class="create-layout">
      <form class="create-form" onSubmit={submit}>
        <Card class="create-section create-description"><div class="create-section-top"><div class="step-number">01</div><div><h2>Описание товара</h2><p>Расскажите о товаре — предложим категорию КТРУ и заполним характеристики.</p></div></div>
          <div class="create-section-body">
            <TextField class="create-field" name="product-description" value={description()} onChange={setDescription}><TextFieldLabel>Описание</TextFieldLabel><TextFieldTextarea id="product-description" rows={6} placeholder="Опишите назначение, материал, размеры, комплектацию и другие особенности товара…" maxlength={3000} /><TextFieldDescription>Чем точнее описание, тем полезнее подсказки.</TextFieldDescription></TextField>
            <div class="description-bottom"><span>{description().length} / 3000</span></div>
            <div class="autofill-panel"><div class="autofill-icon"><WandSparkles size={20} /></div><div class="autofill-copy"><strong>Заполнение по описанию</strong><span>Подберём позицию КТРУ из каталога и перенесём поля характеристик.</span></div><Button type="button" variant="secondary" size="sm" disabled={!description().trim()} onClick={() => void generate()}><Sparkles size={15} />Заполнить</Button></div>
            <div class="autofill-result" role="status" aria-live="polite">
              <Show when={attempted()}>
                <Show when={selected()} fallback={<p class="autofill-hint">Подходящая позиция КТРУ не найдена автоматически. Выберите категорию вручную в дереве.</p>}>
                  {node => <div class="ktru-suggestion"><div class="ktru-suggestion-heading"><Check size={16} /><strong>Выбранная категория КТРУ</strong><Badge variant="secondary">Из каталога</Badge></div><p class="ktru-suggestion-name">{node().name}</p><p class="ktru-suggestion-code">Код КТРУ: <span>{node().code}</span></p></div>}
                </Show>
                <Show when={outdated()}><p class="autofill-hint">Описание или категория изменились. Нажмите «Заполнить» ещё раз, чтобы обновить результат.</p></Show>
              </Show>
            </div>
          </div>
        </Card>
        <Card class="create-section"><div class="create-section-top"><div class="step-number">02</div><div><h2>Основная информация</h2><p>Укажите название и реквизиты товара, проверьте категорию КТРУ.</p></div></div>
          <div class="create-section-body">
            <TextField class="create-field" name="product-name" value={name()} onChange={setName} required><TextFieldLabel>Наименование <span class="required-mark">*</span></TextFieldLabel><TextFieldInput id="product-name" required placeholder="Например, бумага офисная А4, 80 г/м²" maxlength={200} /><TextFieldDescription>Укажите вид товара и его основные отличительные признаки.</TextFieldDescription></TextField>
            <div class="create-category"><div class="category-heading"><div class="category-icon"><Sparkles size={18} /></div><div><strong>Категория КТРУ</strong><span>Иерархия ОКПД2 → КТРУ, быстрый поиск и описание позиции</span></div></div>
              <div class="ktru-selected-box">
                <Show when={selected()} fallback={<p class="autofill-hint" style={{ margin: 0 }}>Категория не выбрана. Откройте каталог КТРУ.</p>}>
                  {node => <>
                    <p class="ktru-suggestion-name" style={{ margin: "0 0 4px" }}>{node().name}</p>
                    <p class="ktru-suggestion-code" style={{ margin: 0 }}>Код: <span>{node().code}</span>{node().status ? ` · ${node().status}` : ""}</p>
                    <p class="category-path">{categoryPath()}</p>
                  </>}
                </Show>
              </div>
              <div class="create-heading-actions" style={{ "justify-content": "flex-start", "padding-bottom": 0, "margin-top": "14px" }}>
                <Button type="button" variant="secondary" size="sm" onClick={() => openPicker(description().trim().slice(0, 80))}><ClipboardList size={15} />Выбрать категорию КТРУ</Button>
              </div>
              <input type="hidden" name="product-category" value={selected()?.code ?? ""} required />
            </div>
            <div class="create-fields-row"><TextField class="create-field" name="product-sku" value={sku()} onChange={setSku} required><TextFieldLabel>Артикул <span class="required-mark">*</span></TextFieldLabel><TextFieldInput id="product-sku" required placeholder="Ваш код товара" maxlength={80} /><TextFieldDescription>Уникальный код в вашем учёте</TextFieldDescription></TextField><TextField class="create-field" name="product-model" value={model()} onChange={setModel}><TextFieldLabel>Модель <span class="optional-mark">необязательно</span></TextFieldLabel><TextFieldInput id="product-model" placeholder="Например, Premium A4" maxlength={120} /></TextField></div>
            <TextField class="create-field" name="product-brand" value={brand()} onChange={setBrand}><TextFieldLabel>Бренд <span class="optional-mark">необязательно</span></TextFieldLabel><TextFieldInput id="product-brand" placeholder="Укажите, если есть" maxlength={120} /></TextField>
          </div>
        </Card>
        <Card class="create-section"><div class="create-section-top"><div class="step-number">03</div><div><h2>Характеристики</h2><p>Поля из выбранной позиции КТРУ; значения можно поправить вручную.</p></div></div>
          <div class="create-section-body">
            <Show when={!characteristics().length}><p class="autofill-hint">Выберите категорию КТРУ или нажмите «Заполнить», чтобы получить поля характеристик.</p></Show>
            <Show when={characteristics().length}><div class="characteristic-heading"><span>{outdated() ? "Описание изменилось — заполните заново, чтобы обновить подсказки" : `Полей: ${characteristics().length}, заполнено: ${filledCount()}`}</span><Badge variant="outline">Можно исправить</Badge></div><div class="characteristic-fields"><Index each={characteristics()}>{item => <TextField class="create-field" value={item().value} onChange={(value) => updateCharacteristic(item().name, value)}><TextFieldLabel>{item().name}</TextFieldLabel><TextFieldInput placeholder="Укажите значение" /></TextField>}</Index></div></Show>
          </div>
        </Card>
        <div class="create-form-footer"><div class="footer-note"><Info size={15} />Предпросмотр не сохраняет и не отправляет данные.</div><Button type="submit" size="lg">Посмотреть карточку <ArrowRight size={17} /></Button></div>
        <Show when={preview()}><Card id="draft-preview" class="preview-card" aria-live="polite"><div class="preview-top"><div><span class="preview-eyebrow">ПРЕДПРОСМОТР · НЕ СОХРАНЕНО</span><h2>{name().trim()}</h2></div><Badge variant="secondary">Черновик</Badge></div><p class="preview-category">{categoryPath() || "Категория не выбрана"}</p><dl class="preview-details"><div><dt>Артикул</dt><dd>{sku().trim()}</dd></div><Show when={model().trim()}><div><dt>Модель</dt><dd>{model().trim()}</dd></div></Show><Show when={brand().trim()}><div><dt>Бренд</dt><dd>{brand().trim()}</dd></div></Show><For each={characteristics().filter((item) => item.value.trim())}>{item => <div><dt>{item.name}</dt><dd>{item.value.trim()}</dd></div>}</For></dl><Show when={description().trim()}><p class="preview-description">{description().trim()}</p></Show><p class="preview-disclaimer">Только локальный предпросмотр. Эта карточка не опубликована.</p></Card></Show>
      </form>
      <aside class="create-aside" aria-label="Подсказки по заполнению">
        <Card class="help-card">
          <div class="help-card-title"><FileText size={19} /><h2>Вопросы и ответы</h2></div>
          <div class="faq-list"><For each={questions}>{item => <details class="faq-item"><summary>{item.question}<ChevronDown size={16} /></summary><p>{item.answer}</p></details>}</For></div>
        </Card>
      </aside>
    </div>

    <Dialog open={pickerOpen()} onOpenChange={setPickerOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Выбор категории КТРУ</DialogTitle>
          <DialogDescription>Найдите позицию по дереву ОКПД2 или быстрому поиску. Подтверждается только лист КТРУ.</DialogDescription>
        </DialogHeader>
        <KtruPickerBody selected={pickerSelected()} onSelect={setPickerSelected} initialFilter={pickerFilter()} />
        <DialogFooter>
          <KtruPickerActions selected={pickerSelected()} onCancel={() => setPickerOpen(false)} onConfirm={confirmPicker} />
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </main>;
}
