import { Title } from "@solidjs/meta";
import { ArrowRight, Check, Package, Search } from "lucide-solid";
import { createSignal, For, type JSX } from "solid-js";
import {
  Badge, Button, Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle,
  Skeleton, StatePanel, TextField, TextFieldDescription, TextFieldErrorMessage, TextFieldInput, TextFieldLabel,
} from "~/ui-kit";

const buttonStyles = ["default", "outline", "secondary", "ghost", "link"] as const;
const badgeStyles = ["default", "secondary", "outline"] as const;
const colors = ["background", "foreground", "card", "primary", "secondary", "muted-foreground", "border", "input", "accent", "ring", "destructive"] as const;

function Example(props: { title: string; description: string; children: JSX.Element }) {
  return <section class="space-y-5" aria-label={props.title}>
    <div><h2 class="text-xl font-semibold">{props.title}</h2><p class="mt-1 text-sm text-muted-foreground">{props.description}</p></div>
    {props.children}
  </section>;
}

export default function UiKitPage() {
  const [query, setQuery] = createSignal("");
  const [submitted, setSubmitted] = createSignal("");
  const [count, setCount] = createSignal(0);
  return <main id="main-content" class="mx-auto w-full max-w-6xl space-y-12 px-5 py-10 sm:px-8">
    <Title>UI-kit — Каталог</Title>
    <header class="space-y-3">
      <Badge variant="secondary">SolidJS · Tailwind CSS · Kobalte</Badge>
      <h1 class="font-heading text-4xl sm:text-5xl">UI-kit проекта</h1>
      <p class="max-w-2xl text-muted-foreground">Базовые компоненты и тема для новых экранов. Все примеры используют публичный импорт из ~/ui-kit.</p>
    </header>

    <Example title="Цвета и типографика" description="Семантические CSS-токены в tokens.css. Тему можно переопределять через переменные --ui-*.">
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <For each={colors}>{color => <div class="overflow-hidden rounded-lg border border-input bg-card">
          <div class="h-16 border-b border-input" style={{ "background-color": `var(--ui-${color})` }} />
          <p class="break-all p-3 font-mono text-xs">{color}</p>
        </div>}</For>
      </div>
      <Card><CardContent class="space-y-2 pt-6"><p class="font-heading text-3xl">Заголовок · Georgia</p><p class="text-sm">Основной текст · Inter / системный sans-serif</p><p class="text-sm text-muted-foreground">Второстепенный текст · muted-foreground</p></CardContent></Card>
    </Example>

    <Example title="Button" description="Пять вариантов, четыре размера, иконки, disabled и полиморфные ссылки.">
      <div class="flex flex-wrap gap-3"><For each={buttonStyles}>{variant => <Button variant={variant} onClick={() => setCount(count() + 1)}>{variant}</Button>}</For></div>
      <div class="flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={() => setCount(count() + 1)}>Маленькая</Button>
        <Button onClick={() => setCount(count() + 1)}>Обычная<ArrowRight /></Button>
        <Button size="lg" onClick={() => setCount(count() + 1)}>Большая</Button>
        <Button size="icon" aria-label="Подтвердить" onClick={() => setCount(count() + 1)}><Check /></Button>
        <Button disabled>Недоступна</Button>
        <Button as="a" href="/" variant="link">Перейти в каталог</Button>
      </div>
      <p class="text-sm text-muted-foreground" role="status">Нажатий: {count()}</p>
    </Example>

    <Example title="Badge" description="Короткие подписи и статусы: default, secondary, outline.">
      <div class="flex flex-wrap gap-3"><For each={badgeStyles}>{variant => <Badge variant={variant}>{variant}</Badge>}</For></div>
    </Example>

    <Example title="TextField" description="Доступные label, подсказка и ошибка. Управляемое значение, поиск, disabled и readonly.">
      <div class="grid items-start gap-6 md:grid-cols-2">
        <form class="space-y-3" onSubmit={event => { event.preventDefault(); setSubmitted(query().trim()); }}>
          <TextField name="query" value={query()} onChange={setQuery}>
            <TextFieldLabel>Поисковый запрос</TextFieldLabel>
            <TextFieldInput type="search" placeholder="Например, бумага А4" />
            <TextFieldDescription>Введите текст и нажмите «Найти».</TextFieldDescription>
          </TextField>
          <Button type="submit"><Search />Найти</Button>
          <p class="text-sm text-muted-foreground" role="status">Запрос: {submitted() || "ещё не отправлен"}</p>
        </form>
        <TextField validationState="invalid" defaultValue="">
          <TextFieldLabel>Название</TextFieldLabel>
          <TextFieldInput placeholder="Обязательное поле" />
          <TextFieldErrorMessage>Укажите название.</TextFieldErrorMessage>
        </TextField>
        <TextField disabled defaultValue="Недоступное значение">
          <TextFieldLabel>Недоступное поле</TextFieldLabel><TextFieldInput />
        </TextField>
        <TextField readOnly defaultValue="Только для чтения">
          <TextFieldLabel>Поле только для чтения</TextFieldLabel><TextFieldInput />
        </TextField>
      </div>
    </Example>

    <Example title="Card" description="Композиция из Header, Title, Description, Content и Footer. Содержимое задаёт экран.">
      <Card class="max-w-lg">
        <CardHeader><CardTitle>Пример карточки</CardTitle><CardDescription>Контейнер без зависимости от товаров или API.</CardDescription></CardHeader>
        <CardContent><p class="text-sm">Используйте карточку для форм, блоков настроек и сводной информации.</p></CardContent>
        <CardFooter><Button variant="outline" onClick={() => setCount(count() + 1)}>Действие<ArrowRight /></Button></CardFooter>
      </Card>
    </Example>

    <Example title="StatePanel" description="Пустое состояние и ошибка с произвольной иконкой, текстом и действием.">
      <div class="grid gap-6 md:grid-cols-2">
        <StatePanel title="Пока ничего нет" description="Добавьте первый элемент, чтобы начать работу." icon={<Package size={25} />} action={<Button variant="outline" onClick={() => setCount(count() + 1)}>Добавить</Button>} />
        <StatePanel variant="error" title="Не удалось загрузить" description="Попробуйте повторить действие." action={<Button variant="outline" onClick={() => setCount(count() + 1)}>Повторить</Button>} />
      </div>
    </Example>

    <Example title="Skeleton" description="Произвольная форма через class; скрыт от скринридера, анимация учитывает prefers-reduced-motion.">
      <Card class="max-w-lg" role="status" aria-label="Пример состояния загрузки">
        <CardContent class="space-y-4 pt-6"><Skeleton class="h-36 w-full rounded-lg" /><Skeleton class="h-4 w-3/4" /><Skeleton class="h-4 w-1/2" /></CardContent>
      </Card>
    </Example>
  </main>;
}
