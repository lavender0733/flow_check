"""把 Claude Code 的 .jsonl 對話記錄轉成可讀的 Markdown。

用法：python export.py <來源.jsonl> <輸出.md>
"""
import json
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])

# 工具呼叫的結果通常又長又不適合閱讀（整份檔案內容、測試輸出等），
# 只留一行摘要；真正有資訊的是使用者與 Claude 的往返。
MAX_TOOL_INPUT = 400
MAX_TOOL_RESULT = 600


def ts(raw):
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ""


def clip(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (省略 {len(text) - limit:,} 字)"


def render_tool_use(block):
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    # 挑出最能代表這次呼叫的欄位
    for key in ("command", "file_path", "pattern", "prompt", "description", "skill"):
        if key in inp:
            val = clip(inp[key], MAX_TOOL_INPUT)
            return f"> 🔧 **{name}** · `{key}`\n>\n> ```\n{chr(10).join('> ' + l for l in val.splitlines())}\n> ```"
    return f"> 🔧 **{name}**"


def render_content(content):
    """回傳 (markdown 段落清單, 是否有實質文字)"""
    parts, has_text = [], False
    if isinstance(content, str):
        s = content.strip()
        return ([s], bool(s)) if s else ([], False)
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            s = (block.get("text") or "").strip()
            if s:
                parts.append(s)
                has_text = True
        elif t == "thinking":
            s = (block.get("thinking") or "").strip()
            if s:
                body = "\n".join("> " + l for l in clip(s, 1500).splitlines())
                parts.append(f"<details><summary>💭 思考</summary>\n\n{body}\n\n</details>")
        elif t == "tool_use":
            parts.append(render_tool_use(block))
        elif t == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            s = clip(c, MAX_TOOL_RESULT)
            if s:
                body = "\n".join("> " + l for l in s.splitlines())
                parts.append(f"> 📄 結果：\n{body}")
    return parts, has_text


def main():
    rows = []
    with SRC.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    out = []
    first_ts = last_ts = ""
    n_user = n_asst = 0

    for row in rows:
        typ = row.get("type")
        if typ not in ("user", "assistant"):
            continue
        msg = row.get("message") or {}
        content = msg.get("content")
        parts, has_text = render_content(content)
        if not parts:
            continue

        stamp = ts(row.get("timestamp"))
        if stamp:
            first_ts = first_ts or stamp
            last_ts = stamp

        if typ == "user":
            # 系統提醒、工具結果回填不算真正的使用者發言
            raw = json.dumps(content, ensure_ascii=False)
            if "<system-reminder>" in raw or "tool_result" in raw:
                if not has_text:
                    continue
            if has_text:
                n_user += 1
            out.append(f"\n---\n\n### 👤 使用者 · {stamp}\n")
        else:
            if has_text:
                n_asst += 1
            out.append(f"\n### 🤖 Claude · {stamp}\n")

        out.extend(parts)
        out.append("")

    header = [
        "# flow_check 架構優化 — 完整對話記錄",
        "",
        f"- 來源：`{SRC.name}`",
        f"- 期間：{first_ts} ～ {last_ts}",
        f"- 使用者發言 {n_user} 則、Claude 回覆 {n_asst} 則",
        f"- 匯出時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "> 工具呼叫與其結果已摘要成引言區塊（只留指令與前幾行輸出），",
        "> 完整內容請看 git 歷史與 `doc/plans/`、`doc/specs/` 底下的文件。",
        "",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(header) + "\n".join(out), encoding="utf-8")
    print(f"已寫出 {OUT}")
    print(f"  {OUT.stat().st_size:,} bytes · 使用者 {n_user} 則 / Claude {n_asst} 則")


if __name__ == "__main__":
    main()
