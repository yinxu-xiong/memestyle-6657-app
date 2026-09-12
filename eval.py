# -*- coding: utf-8 -*-
"""
eval.py — 跑 golden_set.json 全量评测（intensity=2）

- 断点续跑：每完成一条追加写入 eval_output.md 并记入 eval_progress.json，重跑跳过已完成 id
- 不做任何自动评分/自动对比，判断由用户做
"""
import json
import time
from pathlib import Path

from memestyle import runmeme

BASE = Path(__file__).resolve().parent
GOLDEN_FILE = BASE / "golden_set.json"
OUTPUT_FILE = BASE / "eval_output.md"
PROGRESS_FILE = BASE / "eval_progress.json"

INTENSITY = 2


def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8")).get("completed", {})
    return {}


def save_progress(done):
    PROGRESS_FILE.write_text(
        json.dumps({"completed": done}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def append_output(block):
    with OUTPUT_FILE.open("a", encoding="utf-8") as f:
        f.write(block)


def main():
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    done = load_progress()

    if not OUTPUT_FILE.exists():
        OUTPUT_FILE.write_text(
            "# memestyle 评测输出（intensity=2）\n\n"
            "说明：语境对照为并排展示，不做自动对比/评分，判断由人工完成。\n",
            encoding="utf-8",
        )

    print(f"评测开始：golden_set 共 {len(golden)} 条，已完成 {len(done)} 条，"
          f"本次待跑 {sum(1 for g in golden if str(g['id']) not in done)} 条\n")

    for item in golden:
        iid = str(item["id"])
        if iid in done:
            continue

        res = runmeme(item["input"], intensity=INTENSITY)

        model_ctx = res.get("context") if res.get("context") else "ERROR"
        if res.get("error"):
            output = f"ERROR（{res['error']}）"
        else:
            output = (res.get("reply") or "").strip()

        pool_note = ""
        if res.get("context") and res["context"] != "不适合玩梗":
            pool_note = (f"（检索池语境={res.get('context_used_for_pool')}，"
                         f"池{res.get('pool_size')}条/候选{res.get('cand_count')}条）")

        block = (
            f"\n## #{item['id']}\n"
            f"- 输入：{item['input']}\n"
            f"- 期望方向：{item['expected_direction']}\n"
            f"- 语境对照：golden={item['context']} | 模型判断={model_ctx} {pool_note}\n"
            f"- 实际输出：\n\n> {output}\n"
        )
        append_output(block)
        done[iid] = {"model_context": model_ctx, "output": output}
        save_progress(done)
        print(f"[{len(done)}/{len(golden)}] #{item['id']} 完成"
              f"（golden={item['context']} | 模型={model_ctx}）")

        time.sleep(0.5)  # 条与条之间稍作间隔

    print(f"\n评测完成：{len(done)}/{len(golden)} 条，结果见 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
