# -*- coding: utf-8 -*-
"""
app.py — 6657 串子生成器 Web 界面（Streamlit 单文件）

启动（局域网可访问，手机同 WiFi 打开）：
  python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true

依赖：streamlit（requirements.txt 已列）；DEEPSEEK_API_KEY 环境变量（与 memestyle.py 共用）。
交互：一次生成 3 条候选（各基于不同主梗），点选最好的一条；
     选择结果 {时间, 输入, 语境, 浓度, 候选数组, 用户选择(1/2/3/"都不行")} 追加写 feedback.jsonl。
"""
import json
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from memestyle import runmeme_candidates

BASE_DIR = Path(__file__).resolve().parent
FEEDBACK_FILE = BASE_DIR / "feedback.jsonl"
QUOTA_FILE = BASE_DIR / "quota.json"
DAILY_LIMIT = 300  # 每日生成次数上限（所有用户共享，防 API 配额烧穿）

# Streamlit Cloud 部署时从 secrets 读 key；本地跑从环境变量读
if "DEEPSEEK_API_KEY" not in os.environ:
    try:
        os.environ["DEEPSEEK_API_KEY"] = st.secrets["DEEPSEEK_API_KEY"]
    except (KeyError, FileNotFoundError):
        pass  # 两个都没有时由 memestyle.get_client() 报明确错误


def check_quota():
    """按日期计数，超限返回 False。计数存本地 quota.json（重启重置可接受）。
    用 st.session_state 缓存当天计数，减少同会话读盘次数。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cached = st.session_state.get("quota")
    if cached is None or cached.get("date") != today:
        try:
            cached = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            cached = {}
        if cached.get("date") != today:  # 跨天 → 重置
            cached = {"date": today, "count": 0}
        st.session_state.quota = cached
    return cached["count"] < DAILY_LIMIT


def bump_quota():
    q = st.session_state.quota
    q["count"] += 1
    st.session_state.quota = q
    try:
        QUOTA_FILE.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 只读文件系统（Streamlit Cloud）下写失败不拦生成


st.set_page_config(page_title="6657 串子生成器", page_icon="🐽", layout="centered")
st.title("🐽 6657 串子生成器")
st.caption("梗库 23701 条 · 一次出 3 条候选，点选最好的一条")

if not check_quota():
    st.error("今日额度用完，明天再来 🐷")
    st.stop()

with st.form("gen_form"):
    text = st.text_input("弹幕输入", placeholder="例：玩机器提及选手 niko")
    intensity = st.slider("浓度", min_value=1, max_value=3, value=2,
                          help="1 点到为止 · 2 正常浓度 · 3 火力全开")
    submitted = st.form_submit_button("生成", type="primary", use_container_width=True)

if submitted:
    if not text.strip():
        st.warning("先输入一条弹幕再生成")
    else:
        with st.spinner("串子正在翻梗库（判断+检索 1 次，并行生成 3 条，约 10~30 秒）…"):
            res = runmeme_candidates(text.strip(), intensity, n=3)
        if res.get("error"):
            st.error(f"生成失败：{res['error']}")
        else:
            bump_quota()  # 成功才计数
            st.session_state.setdefault("history", []).append({
                "key": str(len(st.session_state.history)),
                "input": text.strip(),
                "candidates": res["candidates"],
                "context": res["context"],
                "intensity": intensity,
                "choice": None,
            })


def append_feedback(item, choice):
    rec = {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "输入": item["input"],
        "语境": item["context"],
        "浓度": item["intensity"],
        "候选": item["candidates"],
        "用户选择": choice,  # 1 / 2 / 3 / "都不行"
    }
    try:
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 云端只读文件系统下写失败不拦选择操作
    item["choice"] = choice


if "history" not in st.session_state:
    st.session_state.history = []

st.divider()
st.subheader("本次会话记录")

for item in reversed(st.session_state.history):
    with st.container(border=True):
        st.markdown(f"**输入**　{item['input']}")
        st.caption(f"语境 {item['context']} · 浓度 {item['intensity']}")
        cols = st.columns(3)
        for idx, (col, cand) in enumerate(zip(cols, item["candidates"]), start=1):
            with col:
                st.markdown(f"**{idx}.** {cand}")
                if st.button("就用这条", key=f"pick_{item['key']}_{idx}",
                             disabled=bool(item["choice"]), use_container_width=True):
                    append_feedback(item, idx)
                    st.rerun()
        if st.button("都不行 👎", key=f"none_{item['key']}",
                     disabled=bool(item["choice"]), use_container_width=True):
            append_feedback(item, "都不行")
            st.rerun()
        if item["choice"]:
            ch = item["choice"]
            st.caption("已记录：都不行" if ch == "都不行" else f"已记录：选了第 {ch} 条")
