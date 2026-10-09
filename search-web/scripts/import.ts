import { openDatabase } from "../src/lib/server/database";
import { sourceDatabasePath } from "../src/lib/server/config";
import { importProducts } from "../src/lib/server/importer";

const catalog = openDatabase();
try {
  const started = performance.now();
  const count = importProducts(catalog, sourceDatabasePath(), (count) => {
    if (count % 5000 === 0) console.log(`Импортировано ${count.toLocaleString("ru-RU")} карточек`);
  });
  console.log(`Готово: ${count.toLocaleString("ru-RU")} карточек за ${((performance.now() - started) / 1000).toFixed(1)} с`);
} finally {
  catalog.sqlite.close();
}
