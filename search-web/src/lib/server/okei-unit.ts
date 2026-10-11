import Database from "better-sqlite3";
import { shortUnitName } from "../characteristic-options";
import { classifiersSqlitePath } from "./config";

let database: Database.Database | undefined;

/** Read nomenclature locally while assembling a record, never make a per-field RPC. */
export function getOkeiUnit(code: string): { code: string; name: string; short: string } | undefined {
  if (!code) return undefined;
  database ??= new Database(classifiersSqlitePath(), { readonly: true, fileMustExist: true });
  const row = database.prepare("SELECT name FROM okei WHERE code=? AND kind='unit' LIMIT 1")
    .get(code) as { name: string } | undefined;
  if (!row) return undefined;
  return { code, name: row.name, short: shortUnitName(row.name) };
}
