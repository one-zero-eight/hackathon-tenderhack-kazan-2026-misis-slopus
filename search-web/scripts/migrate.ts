import { openDatabase } from "../src/lib/server/database";
import { searchDatabasePath } from "../src/lib/server/config";

const { sqlite } = openDatabase();
console.log(`Готово: ${searchDatabasePath()}`);
sqlite.close();
