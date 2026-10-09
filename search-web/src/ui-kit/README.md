# UI-kit

Переиспользуемый модуль для SolidJS с доступными примитивами Kobalte и Tailwind CSS 4.
Визуальные примеры и интерактивные состояния доступны на `/ui-kit`.

## Подключение

В проекте стили уже подключены один раз через `src/app.css`. Компоненты импортируйте
из публичной точки входа, без обращения к внутренним файлам:

```tsx
import { Button, Card, CardContent, TextField, TextFieldInput, TextFieldLabel } from "~/ui-kit";

export function Example() {
  return <Card>
    <CardContent class="space-y-4 pt-6">
      <TextField name="name">
        <TextFieldLabel>Название</TextFieldLabel>
        <TextFieldInput placeholder="Введите название" />
      </TextField>
      <Button type="submit">Сохранить</Button>
    </CardContent>
  </Card>;
}
```

## Публичные компоненты

| Компонент | Интерфейс |
| --- | --- |
| `Button` | `variant`: `default`, `outline`, `secondary`, `ghost`, `link`; `size`: `default`, `sm`, `lg`, `icon`. Поддерживает `disabled`, обработчики, `ref` и `as`. Для иконки без текста задайте `aria-label`. |
| `Badge` | `variant`: `default`, `secondary`, `outline`; стандартные свойства `div`. |
| `Card` | `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter`; стандартные свойства соответствующих HTML-элементов. |
| `TextField` | `value` / `onChange` или `defaultValue`, `disabled`, `readOnly`, `validationState`. Состав: `TextFieldLabel`, `TextFieldInput`, `TextFieldDescription`, `TextFieldErrorMessage`. `TextFieldInput` поддерживает `type="text"` и `type="search"`. |
| `StatePanel` | `title`, `description?`, `icon?`, `action?`, `children?`, `variant`: `default` или `error`. Ошибка получает `role="alert"`; содержимое и действия задаёт потребитель. |
| `Skeleton` | Стандартные свойства `div`. Размеры задаются через `class`; по умолчанию `aria-hidden="true"`. Задайте `role="status"` и описание загрузки на внешнем контейнере. |

Также экспортируются типы props для Button, Badge, TextField и его частей, StatePanel,
Skeleton, функции `buttonVariants`, `badgeVariants` и `cn` для объединения классов.
Для ссылок используйте `<Button as="a" href="…">` либо примените `buttonVariants()`
к компоненту ссылки своего роутера. `class` дополняет базовые стили; конфликтующие
Tailwind-классы объединяются через `tailwind-merge`.

## Тема

`tokens.css` содержит семантические цвета, шрифты и радиусы с префиксом `--ui-`.
Они связаны с Tailwind-токенами: `bg-primary`, `text-muted-foreground`, `border-input`,
`font-heading`, `rounded-lg` и другими. Меняйте CSS-переменные после импорта стилей:

```css
@import "./ui-kit/styles.css";

:root {
  --ui-primary: #285a40;
  --ui-ring: #285a40;
  --ui-font-heading: Georgia, serif;
}
```

Одинаковые переменные можно переопределить на контейнере для локальной темы.
`styles.css` включает Tailwind, базовые стили страницы, keyboard focus и стили
StatePanel/Skeleton. Подключайте его один раз; повторный импорт Tailwind не нужен.

## Использование в другом фронтенде

Скопируйте весь каталог `src/ui-kit` в SolidJS-проект. Внутри модуля используются
только относительные импорты; зависимостей от `~/lib`, роутера, каталога, API и БД нет.
Понадобятся зависимости из текущего `package.json`: `solid-js`, `@kobalte/core`,
`class-variance-authority`, `clsx`, `tailwind-merge`, `tailwindcss` и интеграция
Tailwind 4 с вашей сборкой (в этом проекте `@tailwindcss/vite`).
Подключите `ui-kit/styles.css` в корне приложения и убедитесь, что Tailwind сканирует
TSX-файлы модуля. Это исходный модуль для SolidJS, отдельная сборка или публикация
npm-пакета не требуется.

Новые базовые компоненты добавляйте сюда и экспортируйте из `index.ts`.
Предметные компоненты (`ProductCard`, `Gallery`, поиск и вызовы API) остаются в
приложении. Утилита форматирования дат и чисел также относится к приложению.
Настройки генератора Solid UI в `components.json` указывают на UI-kit;
после генерации добавляйте экспорт и пример на `/ui-kit`.

Button, Badge, Card и TextField адаптированы из `stefan-karger/solid-ui` (MIT).
Комментарии с указанием первоисточника сохранены в файлах компонентов.
