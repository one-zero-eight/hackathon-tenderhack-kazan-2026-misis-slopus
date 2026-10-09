import { spawn } from "node:child_process";

const executable = process.env.MEOW_EMBED_BIN ?? "meow-embed";
const child = spawn(executable, [
  "--SentenceTransformer", "sergeyzh/BERTA", '{"device":"cuda","model_kwargs":{"torch_dtype":"float32"}}',
  "--host", "127.0.0.1", "--port", "8067",
], {
  stdio: "inherit",
  env: { ...process.env, OMP_NUM_THREADS: "4", MKL_NUM_THREADS: "4", TOKENIZERS_PARALLELISM: "false" },
});
child.on("error", (error) => {
  console.error(`Не удалось запустить ${executable}: ${error.message}. Установите meow-embed[server] или задайте MEOW_EMBED_BIN.`);
  process.exitCode = 1;
});
child.on("exit", (code) => { process.exitCode = code ?? 1; });
for (const signal of ["SIGINT", "SIGTERM"] as const) process.on(signal, () => child.kill(signal));
