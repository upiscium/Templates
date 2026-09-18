import { strict as assert } from "node:assert";
import { test } from "node:test";
import { greet } from "../src/main.ts";

test("greet returns a useful greeting", () => {
  assert.equal(greet("world"), "Hello, world!");
});
