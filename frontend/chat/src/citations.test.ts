import { expect, it } from "vitest";

import { parseMemoryCitations } from "./citations";

it("将完整记忆引用替换为稳定编号并合并重复引用", () => {
  const result = parseMemoryCitations(
    "第一条。§cited:[mem_1]§ 第二条。§cited:[mem_1]§ 组合。§cited:[mem_2, mem-3]§",
  );

  expect(result.markdown).toBe(
    "第一条。`§memory-citation:1§` 第二条。`§memory-citation:1§` 组合。`§memory-citation:2§`",
  );
  expect(result.citations).toEqual([
    { number: 1, ids: ["mem_1"] },
    { number: 2, ids: ["mem_2", "mem-3"] },
  ]);
});

it("保留未闭合或包含非法ID的引用原文", () => {
  const source = "未闭合 §cited:[mem_1]，非法 §cited:[mem 2]§";

  expect(parseMemoryCitations(source)).toEqual({ markdown: source, citations: [] });
});
