"""在沙箱子进程内执行单个文件写操作。"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path


def _write_file(payload: dict[str, object]) -> str:
    path = Path(str(payload["path"]))
    content = str(payload.get("content") or "")
    if path.exists() and path.is_dir():
        return f"写入文件失败：目标路径是目录：{payload.get('display_path', path)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"已写入 {len(content)} 字节到 {payload.get('display_path', path)}"


def _edit_file(payload: dict[str, object]) -> str:
    path = Path(str(payload["path"]))
    display_path = str(payload.get("display_path") or path)
    if not path.exists():
        return f"错误：文件不存在：{display_path}"
    raw_content = path.read_bytes().decode("utf-8")
    has_bom = raw_content.startswith("\ufeff")
    content = raw_content[1:] if has_bom else raw_content
    old_text = str(payload.get("old_text") or "")
    new_text = str(payload.get("new_text") or "")
    matched = old_text
    replacement = new_text
    if matched not in content and _supports_crlf_compat(content):
        compatible = old_text.replace("\n", "\r\n")
        if compatible in content:
            matched = compatible
            replacement = new_text.replace("\n", "\r\n")
    if matched not in content:
        return "错误：未找到 old_text，请确保与文件内容完全一致。"
    count = content.count(matched)
    replace_all = bool(payload.get("replace_all"))
    if count > 1 and not replace_all:
        return (
            f"警告：old_text 在文件中出现了 {count} 次。"
            "如需全部替换，设 replace_all=true；如需精确定位，请包含更多上下文。"
        )
    new_content = (
        content.replace(matched, replacement)
        if replace_all
        else content.replace(matched, replacement, 1)
    )
    diff = "\n".join(
        difflib.unified_diff(
            content.splitlines(),
            new_content.splitlines(),
            fromfile=f"{display_path} (before)",
            tofile=f"{display_path} (after)",
            lineterm="",
            n=2,
        )
    )
    restored = ("\ufeff" if has_bom else "") + new_content
    path.write_text(restored, encoding="utf-8", newline="")
    replaced_count = count if replace_all else 1
    if diff:
        return (
            f"已成功编辑 {display_path}（替换 {replaced_count} 处）\n\n"
            f"```diff\n{diff}\n```"
        )
    return f"已成功编辑 {display_path}（替换 {replaced_count} 处）"


def _supports_crlf_compat(text: str) -> bool:
    if "\r\n" not in text:
        return False
    without_crlf = text.replace("\r\n", "")
    return "\n" not in without_crlf and "\r" not in without_crlf


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "文件操作请求参数无效"}))
        return 2
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        operation = str(payload.get("operation") or "")
        if operation == "write_file":
            result = _write_file(payload)
        elif operation == "edit_file":
            result = _edit_file(payload)
        else:
            raise ValueError(f"不支持的文件操作：{operation}")
        # stdout 是跨进程协议，ASCII 转义不依赖 Windows 当前控制台代码页。
        print(json.dumps({"ok": True, "result": result}))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
