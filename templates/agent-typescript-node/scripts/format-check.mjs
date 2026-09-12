import { readFile } from "node:fs/promises";
import { glob } from "node:fs/promises";

const files = await glob("{src,tests}/**/*.{ts,mts,cts}");
let failed = false;
for await (const file of files) {
  const text = await readFile(file, "utf8");
  if (text.includes("\t") || /[ \t]+\n/.test(text)) {
    console.error(`${file}: formatting check failed`);
    failed = true;
  }
}
process.exitCode = failed ? 1 : 0;
