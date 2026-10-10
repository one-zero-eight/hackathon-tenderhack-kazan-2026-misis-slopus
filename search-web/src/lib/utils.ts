export const ktruEisHref = (code: string) => `https://zakupki.gov.ru/epz/ktru/ktruCard/commonInfo.html?itemId=${encodeURIComponent(code)}`;

export const formatNumber = (value: number) => new Intl.NumberFormat("ru-RU").format(value);
export const formatShortDate = (value: string) => value.replace(/^(\d{4})-(\d{2})-(\d{2}).*$/, "$3.$2.$1");
export const formatDate = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("ru-RU", { dateStyle: "long" }).format(date);
};
