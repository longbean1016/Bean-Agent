export interface MemoryCitation {
  number: number;
  ids: string[];
}

export interface ParsedMemoryCitations {
  markdown: string;
  citations: MemoryCitation[];
}

const COMPLETE_CITATION = /§cited:\[([^\]\r\n]+)\]§/gu;
const SAFE_MEMORY_ID = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/u;

export function parseMemoryCitations(source: string): ParsedMemoryCitations {
  const citations: MemoryCitation[] = [];
  const numbers = new Map<string, number>();
  const markdown = source.replace(COMPLETE_CITATION, (raw, payload: string) => {
    const ids = payload.split(",").map((value) => value.trim()).filter(Boolean);
    if (!ids.length || ids.some((id) => !SAFE_MEMORY_ID.test(id))) return raw;
    const uniqueIds = [...new Set(ids)];
    const key = uniqueIds.join("\u0000");
    let number = numbers.get(key);
    if (number === undefined) {
      number = citations.length + 1;
      numbers.set(key, number);
      citations.push({ number, ids: uniqueIds });
    }
    return `**[${number}]**`;
  });

  return { markdown, citations };
}
