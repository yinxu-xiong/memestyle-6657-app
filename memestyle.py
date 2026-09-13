# -*- coding: utf-8 -*-
"""
memestyle.py — 斗鱼6657直播间"串子"风格回复生成器

流程（runmeme 三步）：
  1. 语境判断（DeepSeek，t=0.0，八类硬校验，失败按"泛用"）
  2. 候选梗检索（纯 Python：tags_map.json 语境→站方标签→梗池 top30 + 随机3条低频；
     池<10 回退泛用；"不适合玩梗"跳过检索）
  3. 生成（DeepSeek，t=0.9，三档浓度，输出一条中文串子回复）

用法：
  python memestyle.py "测试文本" --intensity 2

API key 从环境变量 DEEPSEEK_API_KEY 读取。
"""
import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "memes_cache.json"
TAGS_MAP_FILE = BASE_DIR / "tags_map.json"
CARDS_FILE = BASE_DIR / "format_cards.json"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"
API_KEY_ENV = "DEEPSEEK_API_KEY"

# 语境判断的八个合法类别（"严肃"在模型侧表现为"不适合玩梗"）
CONTEXT_CATEGORIES = ["神操作", "下饭", "暴怒", "赛事", "整活", "谐音", "泛用", "不适合玩梗"]
DEFAULT_CATEGORY = "泛用"
NOT_SUITABLE = "不适合玩梗"
FALLBACK_CATEGORY = "泛用"

TOP_N = 30            # 热度 top30
RANDOM_N = 3          # 随机低频梗
POOL_MIN = 10         # 池子不足 10 条回退泛用
CAND_TRUNC = 500      # 单条候选梗进 prompt 的长度上限（防超长复读刷屏爆 token）
RELATED_N = 10        # 与输入字面相关（实体/关键词匹配）的梗，优先供借

CLASSIFY_SYSTEM_PROMPT = (
    "你是斗鱼6657（玩机器CS直播间）的资深弹幕串子。给你一条弹幕，判断该用哪类梗接。"
    "只输出以下八个类别名之一，不要任何其他文字、标点或解释："
    "神操作/下饭/暴怒/赛事/整活/谐音/泛用/不适合玩梗。"
    "判断原则：这是娱乐直播间，默认几乎所有弹幕都可以玩梗，宁玩勿怂——"
    "夸选手神级操作=神操作；主播或选手拉胯、下饭=下饭；弹幕对线开骂=暴怒；"
    "选手、战队、赛事话题=赛事；无厘头整活、刷屏、节目效果=整活；玩文字游戏、求谐音=谐音；"
    "日常闲聊、生活琐事、直播间杂谈=泛用。"
    "以下情况照样归玩梗类，不许归不适合玩梗：工作生活琐事（开会、工资、奶茶店关门）、"
    "求网名、催看弹幕、游戏内战术提问（先拆还是先架、怎么打）、"
    "问举报渠道这类明显节目效果的钓鱼，以及一切关于主播本人的内容（咳嗽、嗓子哑、不说话）——"
    "主播的事永远是梗素材。"
    "特别注意翻译请求（如\"帮我把这句话翻译成英文：会议纪要如下\"）——在直播间语境里这是节目效果，"
    "必须归玩梗类（整活），库里就有现成的翻译梗（\"你家孩子翻译题把device全翻译成爷爷了\"），"
    "绝不许因为措辞像任务就归不适合玩梗。"
    "只有两种情况归不适合玩梗：一是真正沉重的现实困境（家人重病住院、本人情绪崩溃求安慰、真实医疗求助如看报告挂科）；"
    "二是语气认真、无梗可接的计算和查证类任务。"
)

# 三档浓度示例（用户指定，原样保留）
INTENSITY_EXAMPLES = {
    1: [
        "鱼越大，鱼刺越大，鱼刺越大，鱼肉越少，鱼肉越少，鱼越小，所以鱼越大，鱼越小。",
        "你们男生说话能不能不要那么粗野啊？直播间还有很多我这样的小萌妹在看啊!",
    ],
    2: [
        "玩美国游戏，听日本歌，要不是看在你支持中国烟草的份上早把你这直播间举办了",
        "许家印:发送他们房子 测谎仪:👎🏻👎🏻👎🏻",
    ],
    3: [
        "领头的羊🐏敏捷的豹🐆善战的狼🐺骁勇的虎🐯远见的鹰🦅忠诚的狗🐕不停的鹿🦌不停的鹿🦌不停的鹿🦌不停的鹿🦌用力的草🌿用力的草🌿用力的草🌿坚挺的牛🐮坚挺的牛🐮坚挺的牛🐮🐳浓稠的鲸🐳浓稠的鲸🐔爆炸的鸡🐔🐍连续",
        "啊啊啊啊啊啊啊宝宝你是一个香香软软甜甜糯糯蜂蜜奶油甜甜腻腻酥酥脆脆滑滑嫩嫩番茄炒可乐番茄炒科比蓝莓苹果香（省略）",
        "这个直播间氛围太好了，就像在恬静的乡下和邻里一起聊家长里短，偶尔还能听到后院的猪叫。",
    ],
}
INTENSITY_NAMES = {1: "点到为止", 2: "正常浓度", 3: "火力全开"}


# ---------------------------------------------------------------- DeepSeek 客户端与统一重试封装
_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise RuntimeError(f"环境变量 {API_KEY_ENV} 未设置")
        _client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    return _client


def call_deepseek(messages, temperature):
    """统一 DeepSeek 调用：失败自动重试 3 次（指数退避 1/2/4s），单次超时 60s。仍失败返回 None。"""
    client = get_client()
    last_err = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[warn] DeepSeek 调用失败(第{attempt + 1}/3次): {e}，{wait}s后重试",
                  file=sys.stderr)
            time.sleep(wait)
    print(f"[error] DeepSeek 连续3次失败: {last_err}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- 数据加载（梗池）
_memes = None
_tags_map = None


def load_data():
    global _memes, _tags_map
    if _memes is None:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        _memes = cache["items"]
        _tags_map = json.loads(TAGS_MAP_FILE.read_text(encoding="utf-8"))
    return _memes, _tags_map


def item_tag_codes(meme):
    return {t.strip() for t in (meme.get("tags") or "").split(",") if t.strip()}


def pool_for_category(category):
    """语境类 → 站方标签 → 梗池；无对应标签或池<10 条时回退泛用池。返回 (实际使用语境, 池)"""
    memes, tags_map = load_data()
    mapping = tags_map["mapping"].get(category, {})
    if not mapping.get("site_tags"):
        # 该语境无对应站方标签（如神操作/谐音）→ 回退
        category = mapping.get("fallback", FALLBACK_CATEGORY)
        mapping = tags_map["mapping"][category]

    tag_set = set(mapping["site_tags"])
    if category == FALLBACK_CATEGORY:
        # 泛用池 = 站方泛用标签 + tags 为空的梗（tags_map.json rules 注明）
        pool = [m for m in memes
                if not item_tag_codes(m) or (tag_set & item_tag_codes(m))]
    else:
        pool = [m for m in memes if tag_set & item_tag_codes(m)]

    if len(pool) < POOL_MIN:
        # 池子不足 → 回退泛用池（含 tags 为空的梗）
        category = FALLBACK_CATEGORY
        gset = set(tags_map["mapping"][FALLBACK_CATEGORY]["site_tags"])
        pool = [m for m in memes
                if not item_tag_codes(m) or (gset & item_tag_codes(m))]
    return category, pool


def pick_candidates(pool, text, rng=random):
    """池内二段检索：一段=池内与输入相关的梗（外号/实体/有区分度 bigram 命中，置顶）；
    二段=热度补足至 TOP_N（避开已选）+ 随机 3 条低频梗。cnt 是字符串，必须转 int。"""
    rel = relevance_rank(pool, text)[:TOP_N]
    rel_ids = {id(m) for m in rel}
    ranked = sorted(pool, key=lambda m: int(m["cnt"] or 0), reverse=True)
    hot_need = max(0, TOP_N - len(rel))
    hot = [m for m in ranked if id(m) not in rel_ids][:hot_need]
    chosen = rel + hot
    rest = ranked[TOP_N:]
    lows = rng.sample(rest, min(RANDOM_N, len(rest))) if rest else []
    return chosen + lows


def format_candidates(cands):
    lines = []
    for m in cands:
        b = (m.get("barrage") or "").strip()
        if len(b) > CAND_TRUNC:
            b = b[:CAND_TRUNC] + "…（复读截断）"
        lines.append(f"- (热度{int(m['cnt'] or 0)}) {b}")
    return "\n".join(lines)


def _norm(s):
    """leet 归一化：m0NESY→monesy、s1mple→simple（0→o、1→i、3→e、5→s）。"""
    s = s.lower()
    for a, b in (("0", "o"), ("1", "i"), ("3", "e"), ("5", "s")):
        s = s.replace(a, b)
    return s


def extract_terms(text):
    """提取输入的匹配词：英文 token（归一化，如 niko、monesy）+ 中文 bigram。"""
    t = _norm(text)
    words = set(re.findall(r"[a-z]{2,}", t))
    bigrams = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(seg) - 1):
            bigrams.add(seg[i:i + 2])
    return words, bigrams


BIGRAM_DF_MAX = 100  # bigram 命中超过此条数的视为泛用词（如"玩机""比赛"），无区分度

# 站内黑话：英文实体 → 中文外号（检索词扩充，映射来自用户确认的评测期望）
NICKNAMES = {
    "dupreeh": ["汤"],
    "device": ["狗蛇", "阿汤"],
    "niko": ["虾哥", "虾线"],
    "monesy": ["尼尼孩孩"],
    "zywoo": ["载物"],
    "twistzz": ["弓箭手", "宫监手"],
    "巴西": ["波哈"],
}

# 场景 → 补充检索词（把库里贴身但字面捞不到的梗补进候选；只放库内真实存在的）
SCENE_HINTS = {
    "漂亮操作": ["吓哭了"],
    "高难度": ["吓哭了"],
    "击杀": ["吓哭了"],
    "吃什么": ["苏哈", "小夫"],
    "吃的": ["苏哈", "小夫"],
    "红线": ["羞死了"],
    "工资": ["发工资"],
    "追分": ["黏住了"],
    "激情": ["哦？"],
    "焦灼": ["哦？"],
    "还行": ["carry"],
    "请假": ["还搁这休息"],
    "翻译": ["翻译题"],
}


def _strong_terms(text):
    """输入命中的外号/场景补充检索词（强信号，不做 df 过滤）。"""
    t = _norm(text)
    strong = set()
    for key, aliases in NICKNAMES.items():
        if key in t:
            strong.update(a.lower() for a in aliases)
    for key, hints in SCENE_HINTS.items():
        if key in text:
            strong.update(h.lower() for h in hints)
    return strong


def relevance_rank(corpus, text):
    """语料内按与输入的相关性降序排序（全库检索与池内二段检索共用）。
    分层：强信号词（外号/场景提示，如输入 dupreeh→"狗蛇""汤"）> 英文实体 >
    有区分度的中文 bigram（df≤BIGRAM_DF_MAX，泛词如"玩机""比赛"不出场）；
    同层按命中数、热度排。"""
    words, bigrams = extract_terms(text)
    strong = _strong_terms(text)
    normed = [(m, _norm(m.get("barrage") or "")) for m in corpus]

    df = {}
    for _, b in normed:
        if not b:
            continue
        for g in bigrams:
            if g in b:
                df[g] = df.get(g, 0) + 1
    keep = {g for g, c in df.items() if c <= BIGRAM_DF_MAX}

    scored = []
    for m, b in normed:
        if not b:
            continue
        s_hit = sum(1 for w in strong if w in b)
        w_hit = any(w in b for w in words)
        g_hits = sum(1 for g in keep if g in b)
        if not s_hit and not w_hit and not g_hits:
            continue
        # 同层内短梗优先（弹幕贴合优先，长 copypasta 只是顺带提到关键词），
        # 长度作平局裁决后再看热度
        scored.append((s_hit, w_hit, g_hits, -len(b), int(m["cnt"] or 0), m))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
    return [s[5] for s in scored]


def related_memes(text, limit=RELATED_N):
    """全库相关梗（跨池）：含外号/场景强信号，供生成最优先借。"""
    memes, _ = load_data()
    return relevance_rank(memes, text)[:limit]


# ---------------------------------------------------------------- 格式卡（V2：把"原句"升级为"骨架"）
_cards = None


def load_cards():
    """加载手写格式卡库。文件缺失时返回空列表（V2 自动降级为仅短梗+风格参考）。"""
    global _cards
    if _cards is None:
        try:
            _cards = json.loads(CARDS_FILE.read_text(encoding="utf-8"))["cards"]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            _cards = []
    return _cards


def card_families(cands, text, top=5):
    """把检索到的候选归入格式卡家族，返回与"输入语义 + 候选构成"最匹配的 top 张卡。

    打分 = 输入命中 provider hints 数 ×3 + 候选命中 pattern 条数（上限5）。
    输入 hints 权重更高：说明这条输入本身就在讲这类场景。"""
    cards = load_cards()
    scored = []
    for c in cards:
        hint = sum(1 for h in c.get("input_hints", []) if h and h in text)
        members = sum(1 for m in cands
                      if any(p in (m.get("barrage") or "") for p in c.get("pattern", [])))
        score = hint * 3 + min(members, 5)
        if score > 0:
            scored.append((1 if hint else 0, hint, min(members, 5), c))
    # 关键：有"输入正向证据（hint）"的卡严格排在没有证据的卡前面，
    # 避免靠语料共现（members）混进来的万能卡抢占首位
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [s[3] for s in scored[:top]]


def format_families(cards):
    """渲染【可用骨架】区块：只给骨架/用法/填法/红线，不给完整原句（防止填充物抢戏）。"""
    if not cards:
        return "（本次未命中现成骨架，直接按下面的短梗或情绪短句接）"
    lines = ["（铁规：选中的骨架必须把 <槽位> 换成输入里的内容。若某张卡的骨架不需要替换任何内容"
             "就能直接发出去，说明这张卡不适用这条输入，换一张——禁止用同一张万能卡套所有输入。）"]
    for i, c in enumerate(cards, 1):
        neg = "；".join(c.get("negative", [])[:2])
        lines.append(f"{i}. 【{c['name']}】\n"
                     f"   骨架：{c['skeleton']}\n"
                     f"   何时用：{c['usage']}\n"
                     f"   怎么填：{c['fill']}"
                     + (f"\n   红线：{neg}" if neg else ""))
    return "\n".join(lines)


def short_fillers(cands, limit=8, max_len=22):
    """对口短梗（≤max_len 字，属于"词"级梗，可原样搬运）。"""
    seen, out = set(), []
    for m in cands:
        b = (m.get("barrage") or "").strip()
        if not b or len(b) > max_len or b in seen:
            continue
        seen.add(b)
        out.append(b)
        if len(out) >= limit:
            break
    return out


def prompt_version():
    """生成层版本开关：MEMESTYLE_PROMPT=v1 立刻切回旧版（软回滚，无需改文件）。"""
    return os.environ.get("MEMESTYLE_PROMPT", "v2").strip().lower() or "v2"


# ---------------------------------------------------------------- 三步主流程
def classify_context(text):
    """第 1 步：语境判断。返回类别名；调用失败返回 None（上层记 ERROR）。"""
    raw = call_deepseek(
        [{"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
         {"role": "user", "content": text}],
        temperature=0.0,
    )
    if raw is None:
        return None
    answer = raw.strip()
    if answer in CONTEXT_CATEGORIES:  # 去空白后精确匹配八类之一
        return answer
    return DEFAULT_CATEGORY  # 校验不过 → 泛用


GEN_SYSTEM_PROMPT = """你是斗鱼6657（玩机器CS直播间）泡了十年的老串子，现在回一条弹幕。你发的是弹幕，不是小作文。

【第零原则：贴合输入，最高优先级，压倒下面一切规则】
你回的是"这条输入"的弹幕：主体一致、场景一致、方向一致。一条梗再好笑，跟输入对不上就是废稿——宁可输出"考"这类纯情绪短句，也绝不输出一条不相关的神梗。好笑是加分项，贴合是及格线。
选梗严格按此顺序：
1. 【相关梗】里找输入实体/场景直接命中的 → 过五道闸 → 搬运；
2. 没有 → 【池内梗】前段（池内相关）里找 → 过闸 → 搬运；
3. 都没有 → 赛事/选手/主播状态类输入回纯情绪短句；带节奏/刷屏/拿不准语义的输入回无意义带节奏向刷屏。
任何一条候选先过完五道闸再考虑好笑程度，不过闸的直接跳过看下一条。
【排位优先】【相关梗】和【池内梗】前段都已按贴合度从高到低排好：从第一条开始依次过闸，取第一条能过闸的直接输出；不许跳过能过闸的前排去选后排（后排只在前面全部过不了闸时才看）。
过闸判定从宽，以下都算贴合（站内传统打法）：数字跨域映射（十万兵↔十万观众、借十万支箭↔凑十万弹幕）、求助类输入接"把你家孩子/你家主播拉出来吐槽"式（翻译求助接"你家孩子翻译题把device全翻译成爷爷了"）。拿不准=过闸。

【弹幕铁律（违反任何一条=废稿）】
1. 极短、纯情绪。真弹幕大多数时候就是一两个词、最多一句话："考""男的""+1"这种就能成一条。第1档一词一句；第2档一两句以内；只有第3档可以复读轰炸和长文学。写完自查：能删的字全删，删到不能再删为止。
2. 禁止完整长句、解说腔、书面框架句式（"你问我…我只能说""既然…那不就是""建议…"一律不许）。
3. 禁止自创梗、禁止编造数字、禁止编造因果逻辑（"哭23次""越激动越追不上"这种自编的都不许）。
4. 默认串子立场：嘲讽主播（玩机器）、看乐子、带节奏；夸操作用老哥式（"牛逼""没关就是开了"），不端着。
5. emoji 是情绪放大器，该用就用（😎😭🤣💀👎），别堆砌。
6. 求助/钓鱼/催看类弹幕（急急急、问渠道、问看法），优先"耍主播"式接梗（例：机器你先别解说了，赶紧看下hltv，出大事了！），而不是认真解答。输入本身是假新闻/诈骗类时，就输出一条具体的同款假新闻，不要输出对它的描述。

【核心打法：搬运，不是改编】
弹幕的最高水平，是认出当前局势对应候选库里的哪条原梗，把它原样搬出来用。你的任务只是"匹配+搬运"，不是创作：
- 【相关梗】里有能对上的 → 优先整条搬运，越还原越好，原文字、词序、标点、emoji 都尽量不动；搬运的原梗多长就多长，不受档位句数限制（复读轰炸/长文学类原梗只在第3档用）。
- 实体对不上必须改名 → 只换核心实体（人名/队名），其余一字不改；格式化梗（XX越多YY越少、@某某：、怀念最XX的…、【HLTV】…）保留完整格式只替换实体。
- 改写=高危动作。模型自己仿写的句子一律没有原梗的逻辑（历轮评测反复证明），改编只许"实体替换"，不许"句式仿写"；没有可搬运的就回纯情绪短句（"考""男的""绷不住了""急什么急"），宁短勿编。禁止复读输入里的原词。
- 梗尾部/内部的重复符号（——————、！！！、emoji 连排、复读段）是原文的一部分，原样保留，不许截掉（截了就像仿写的了）。
- 【池内梗】前段（与输入相关）可以借；后段热门仅看风格。

【选梗前必做：解析输入三要素】
看输入弹幕，先明确三件事，再去看梗库：
- 主体：说的是谁——主播（玩机器）？哪个选手？发弹幕的人自己？别人？
- 场景：在干什么——看比赛 / 打比赛 / 解说 / 直播闲聊 / 日常生活 / 求助。
- 方向：该什么态度——夸（选手/操作漂亮）/ 喷（拉胯）/ 看乐子带节奏 / 关心式整活。

【贴身五道闸（每条候选依次过，任何一道不过就换下一条）】
1. 主体闸：梗里的主角必须和输入主体一致。输入提到具体选手名 → 只搬该选手的梗（device 输入不搬 Twistzz 梗）；输入说"选手打出漂亮操作"没点名 → 只搬夸选手/夸操作的梗，嘲讽玩机器本人的梗（"操作像猪"）一律不许出；输入是弹幕自己求助（工资、网名、吃饭）→ 搬"观众视角接梗"的，不搬主播视角的（玩机器领不到工资 ≠ 你的工资）。
2. 场景闸：看比赛 ≠ 打比赛 ≠ 解说比赛。主播在看比赛时兴奋 ≠ 主播在打比赛（"游龙乱拱"是打比赛/耍宝，不许接"看比赛激动"的输入）；输入明说解说激情 → 禁止再骂解说没激情，极性不能反。
3. 方向闸：方向必须跟输入一致。夸操作只接夸的梗（"吓哭了""牛逼""没关就是开了"），输入求助网名接"给你起一个"向的，不接"你为什么改名"向的。实在方向对不上就回纯情绪短句。
4. 类别宽严闸：赛事、选手、主播状态类输入 → 上面三道闸严格执行；纯带节奏/刷屏/无意义信息类输入（急急急、破十万、平平常常无事发生）→ 可放宽贴身度，怎么好笑怎么来。
5. 搬运零加戏闸：整条搬运只许删不许加。禁止在梗前面垫输入句子的复述（"平平常常无事发生，刘亦博为什么你…"✗，库里原文是"机器 为什么你一不直播就有大事发生？"），禁止在梗后面补解释、补反问。梗原文几个字就是几个字。

【已知错配黑名单（历轮评测实锤，出现即弃用）】
- "游龙/猪圈乱拱"：只用于玩机器自己打游戏耍宝。"看比赛激动/兴奋/追分"的输入禁止接。
- "玩机器领不到工资/被辱骂"系列：主播视角。"弹幕观众自己工资没发"的输入禁止接，主体不同。
- "肚子被玩机器搞大了"：不是"吃了什么"的回答。"中午吃的什么"要选答案型（"吃的苏哈，配的小夫"）。
- 嘲讽玩机器的梗（"操作像猪""过气女优"）：输入是"选手打出漂亮操作"时禁止接，那是夸选手的场合。

【多实体规则】
输入同时提到两个及以上选手/队伍（如 dupreeh 和 device）时，优先搬能同时覆盖两者的梗（如"盘点玩机器最恨的几个人"体），只覆盖一个实体的梗除非语境完全一致否则不选。

【兜底规则】
库里实在没有能过闸的梗（输入场景库内无对口，如"主播怎么不说话"）：不要硬贴、不要自创，输出一条无意义带节奏向刷屏（如"😮哦？😮哦？😮哦？""✋停✋止✋这✋场✋闹✋剧✋吧"），格式可从候选梗里借。

【浓度档位】（三档示例只标定浓度尺度，禁止照搬示例本身当输出，本次只用指定档位）
第1档 点到为止——一词一句，轻碰就走：
{ex1}
第2档 正常浓度——一两句以内：
{ex2}
第3档 火力全开——复读轰炸/长文学攻击，可长：
{ex3}

【硬性要求】
1. 梗必须与输入语义真实相关，宁可不玩也不硬玩。
2. 只输出一条回复，中文，不要任何解释。"""

GEN_SYSTEM_PROMPT_V2 = """你是斗鱼6657（玩机器CS直播间）泡了十年的老串子，现在回一条弹幕。你发的是弹幕，不是小作文。

【核心认知：梗弹幕 = 骨架 + 填充物】
6657 老哥玩梗从来不是背原句，而是"套格式填新料"：骨架（XX越多YY越少、【HLTV】…宣布…、我的青春是…、@某人 教学X）是大家共用的；填充物（里面的人、事、数）是当次现场填的。笑点=格式与内容的落差。
你的活是：认出这条输入该套哪个骨架 → 把输入里的东西填进去。不是从库里挑一条最像的原句整段抄走。

【第一优先级：接地（压倒一切）】
你回的是"这条输入"的弹幕。写完自查三问，答不上就重写：①主体对吗（说的是谁）②场景对吗（在干什么）③方向对吗（夸/喷/看乐子）。
一条梗再有名，跟输入对不上就是废稿。宁可回"考""绷不住了"这种纯情绪短句，也绝不回一条不接地气的神梗。

【怎么产出（严格按序）】
1. 【可用骨架】里挑一个与当前场景最匹配的 → 按"怎么填"把输入的人/事/物填进槽位 → 输出「骨架 + 新填充物」。
   ★ 骨架卡里的"代表样句"是别人的填充物，禁止整段照搬（照搬=填充物抢戏），只借格式。
   ★ 自检：选中的骨架若"不需要替换任何槽位就能原样发出去"，说明这张卡不适用这条输入——必须换一张。同一张万能卡套在所有输入上=废稿（历轮事故：把"🐖宝宝粗现"套在闲聊/请假/沉默等无关输入上）。
2. 【对口短梗】里 ≤20 字的短句（"词"级梗，如 吓哭了 / 哦？ / @karrigan:也许🐶💩才刚刚黏住）可以直接原样搬运。
3. 骨架和短梗都对不上 → 回纯情绪短句（"考""男的""绷不住了""急什么急"）；带节奏/刷屏/拿不准语义的输入 → 回无意义带节奏向刷屏（😮哦？×N、✋停✋止✋…）。
4. 任何情况都不许：整段照搬 20 字以上的原句、自创文学比喻、编数字编因果、复读输入的原词。

【弹幕铁律（违反=废稿）】
1. 极短、纯情绪。第1档一词一句；第2档一两句以内；只有第3档可以复读轰炸和长文学。
2. 禁止完整长句、解说腔、书面框架句式（"你问我…我只能说""建议…"一律不许）。
3. 默认串子立场：嘲讽主播、看乐子、带节奏；夸操作用老哥式（"牛逼""没关就是开了"）。
4. emoji 是情绪放大器，该用就用（😮😭🤣💀👎），别堆砌。
5. 求助/钓鱼/催看类（急急急、问渠道），优先"耍主播"式接梗（例：机器你先别解说了，赶紧看下hltv，出大事了！）。输入是假新闻/诈骗类时，直接输出一条具体的同款假新闻本体，不要输出对它的描述。

【错误示范（负样本，见到即避）】
- 填充物抢戏：把"俯卧撑语录体""我的青春体"套在一条残局/战术输入上——格式对了但内容没接住，等于没回这条输入。
- 主体/方向反：输入在夸"选手打出漂亮操作"，却回"玩机器操作像猪"——主体和方向都错。
- 场景反：输入是"看比赛激动"，却回"游龙/猪圈乱拱"（那是主播自己打游戏耍宝）。
- 主体错位：输入是"弹幕观众说工资没发"，却回"玩机器领不到工资"（那是主播视角）。
- 只抄格式不留内容：整段搬运一条长原句，换谁来都能用——说明没接地。

【浓度档位】（三档示例只标定浓度尺度，禁止照搬示例本身当输出，本次只用指定档位）
第1档 点到为止——一词一句，轻碰就走：
{ex1}
第2档 正常浓度——一两句以内：
{ex2}
第3档 火力全开——复读轰炸/长文学攻击，可长：
{ex3}

【硬性要求】
1. 梗必须与输入语义真实相关，宁可不玩也不硬玩。
2. 只输出一条回复，中文，不要任何解释。"""

# 配对 few-shot（输入 → 回复）：示范"骨架 + 填充物 = 接地"，是 V1 从未测过的最后一块零件。
# 放在 system 之后的真实多轮里，让模型看到"怎么把输入填进骨架"。
FEWSHOT_PAIRS = [
    # 夸选手操作（方向闸：只接夸的；短梗直接搬运）
    ("玩机器解说中选手打出漂亮操作", "童站弹幕：吓哭了"),
    # 主播请假/日常（骨架=@某人教学体，填充物=输入里的"登山"）
    ("玩机器请假说要去登山", "@玩机器 教学登山\n@玩机器 教学嘴硬\n回复:先教教怎么开播吧"),
    # 接住吹捧本身（骨架=残局叙事体，填充物=输入的"登峰造极"）
    ("这把残局登峰造极", "登峰造极？一打二、对面打包、你没钳子，这是对枪型残局吗😧…最后不还是路边了😭"),
    # 生态外输入（骨架=多实体并列比喻体，填的是"签证"语义，不抓词）
    ("帮我看下办签证要准备什么材料", "又一个材料没备齐的，此刻我就像没签证的京介，没带照片的niko，没填表的wdf😅"),
]

NOMEME_SYSTEM_PROMPT = (
    "你是斗鱼6657直播间的热心观众。用户发来一条严肃/正经的弹幕或求助，"
    "请认真正常地回复，不玩任何梗、不阴阳怪气。只输出一条回复，中文，不要解释。"
)


def generate_reply(text, intensity, candidates=None, related=None, assigned=None):
    """第 3 步：生成串子回复。返回回复文本；调用失败返回 None。

    assigned（多候选模式专用，单条模式不传=行为不变）：
      - dict：本条指定主梗（必须以它为核心搬运）
      - "fallback"：本条走兜底向（无意义带节奏/刷屏体）
    candidates=None 时忽略 assigned（不适合玩梗路径没有主梗概念）。
    """
    if candidates is None:
        messages = [
            {"role": "system", "content": NOMEME_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
    elif prompt_version() == "v2":
        return _generate_v2(text, intensity, candidates, related, assigned)
    else:
        system = GEN_SYSTEM_PROMPT.format(
            ex1="\n".join("- " + e for e in INTENSITY_EXAMPLES[1]),
            ex2="\n".join("- " + e for e in INTENSITY_EXAMPLES[2]),
            ex3="\n".join("- " + e for e in INTENSITY_EXAMPLES[3]),
        )
        system += f"\n\n【本次档位】第{intensity}档（{INTENSITY_NAMES[intensity]}）"
        user = f"输入弹幕：{text}"
        if related:
            user += f"\n\n【相关梗】（全库与输入直接命中，最优先从这里借）：\n{format_candidates(related)}"
        user += (f"\n\n【池内梗】（前段=池内与输入相关，可借；后段=该语境热门，仅风格参考）：\n"
                 f"{format_candidates(candidates)}")
        if assigned == "fallback":
            user += ("\n\n【本条指定路线】本条不走主梗：输出一条无意义带节奏向刷屏，"
                     "格式可从上面候选梗里借。")
        elif isinstance(assigned, dict):
            b = (assigned.get("barrage") or "").strip()
            if len(b) > CAND_TRUNC:
                b = b[:CAND_TRUNC] + "…（复读截断）"
            user += (f"\n\n【本条指定主梗】本条回复必须以这条梗为核心（优先整条搬运，"
                     f"实体不符只换实体；若它实在过不了五道闸，走兜底向刷屏）：\n"
                     f"- (热度{int(assigned['cnt'] or 0)}) {b}")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    return call_deepseek(messages, temperature=0.9)


def _generate_v2(text, intensity, candidates, related, assigned=None):
    """V2 生成：骨架卡 + 对口短梗 + 配对 few-shot + 负样本。

    与 V1 的关键差别：不再把 33 条裸原句陈列给模型（那是"填充物抢戏"的根源），
    改为给"可用骨架（只给格式/用法/填法/红线）"+"≤20字对口短梗"，并用配对 few-shot
    示范如何把输入填进骨架（接地）。
    """
    merged = list(related or []) + [m for m in (candidates or [])
                                    if id(m) not in {id(x) for x in (related or [])}]
    cards = card_families(merged, text, top=5)
    fillers = short_fillers(merged)

    system = GEN_SYSTEM_PROMPT_V2.format(
        ex1="\n".join("- " + e for e in INTENSITY_EXAMPLES[1]),
        ex2="\n".join("- " + e for e in INTENSITY_EXAMPLES[2]),
        ex3="\n".join("- " + e for e in INTENSITY_EXAMPLES[3]),
    ) + f"\n\n【本次档位】第{intensity}档（{INTENSITY_NAMES[intensity]}）"

    messages = [{"role": "system", "content": system}]
    for u, a in FEWSHOT_PAIRS:
        messages.append({"role": "user", "content": f"输入弹幕：{u}"})
        messages.append({"role": "assistant", "content": a})

    user = (f"输入弹幕：{text}\n\n"
            f"【可用骨架】（挑一张最贴当前场景的，按“怎么填”把输入内容填进槽位）：\n"
            f"{format_families(cards)}")
    if fillers:
        user += ("\n\n【对口短梗】（≤20字，可原样搬运，也可不用）：\n"
                 + "\n".join("- " + b for b in fillers))
    if assigned == "fallback":
        user += ("\n\n【本条指定路线】本条不走骨架：输出一条无意义带节奏向刷屏"
                 "（如 😮哦？×N、✋停✋止✋…）。")
    elif isinstance(assigned, dict) and "skeleton" in assigned:
        user += (f"\n\n【本条指定骨架】必须用这张骨架、把输入内容填进槽位"
                 f"（禁止照搬别处的填充物）：\n- 【{assigned['name']}】{assigned['skeleton']}")
    elif isinstance(assigned, dict):
        b = (assigned.get("barrage") or "").strip()
        if len(b) > CAND_TRUNC:
            b = b[:CAND_TRUNC] + "…（截断）"
        user += (f"\n\n【本条指定主梗】以它为骨架来填输入内容；若它实在接不住输入，"
                 f"就换【可用骨架】里更贴的一张（不要整段照搬它）：\n- {b}")
    messages.append({"role": "user", "content": user})

    return call_deepseek(messages, temperature=0.9)


def runmeme(text, intensity=2):
    """核心入口。返回 dict：context / reply / pool_size / cand_count / error"""
    result = {"text": text, "intensity": intensity,
              "context": None, "reply": None,
              "pool_size": 0, "cand_count": 0, "error": None}

    # 第 1 步：语境判断
    ctx = classify_context(text)
    if ctx is None:
        result["error"] = "语境判断调用失败（重试3次后仍失败）"
        return result
    result["context"] = ctx

    # 第 2 步：候选梗检索（不适合玩梗 → 跳过，直接正常回复）
    if ctx == NOT_SUITABLE:
        reply = generate_reply(text, intensity, candidates=None)
    else:
        used_ctx, pool = pool_for_category(ctx)
        result["context_used_for_pool"] = used_ctx
        result["pool_size"] = len(pool)
        cands = pick_candidates(pool, text)
        result["cand_count"] = len(cands)
        related = related_memes(text)
        result["related_count"] = len(related)
        reply = generate_reply(text, intensity, candidates=cands, related=related)

    # 第 3 步结果处理
    if reply is None:
        result["error"] = "生成调用失败（重试3次后仍失败）"
        return result
    result["reply"] = reply.strip()
    return result


def runmeme_candidates(text, intensity=2, n=3):
    """多候选入口（Web 端用）：一次生成 n 条候选，用户点选最好的一条。

    与 runmeme 的关系：单条行为完全不变（runmeme/eval.py/CLI 不受影响）；
    本函数复用同一套判断/检索/生成，仅做两点扩展——
    1. 语境判断和候选梗检索只做一次；
    2. 生成阶段并行调 n 次（ThreadPoolExecutor，t=0.9），三条基于不同主梗：
       第 i 条用排位第 i 的候选梗（全库相关在前、池内相关次之，去重）；
       可用主梗不足 n 个时，后面的条走兜底向（无意义带节奏/刷屏体）。
       "能否过闸"由生成模型按 prompt 五道闸判定，代码侧按排位近似指定。

    返回 dict：context / candidates（list[str]，成功的条，≤n）/ pool_size /
    cand_count / assigned（每条的主梗摘要，调试用）/ error
    """
    result = {"text": text, "intensity": intensity,
              "context": None, "candidates": [],
              "pool_size": 0, "cand_count": 0, "assigned": [], "error": None}

    # 第 1 步：语境判断（只做一次）
    ctx = classify_context(text)
    if ctx is None:
        result["error"] = "语境判断调用失败（重试3次后仍失败）"
        return result
    result["context"] = ctx

    # 第 2 步：候选梗检索（只做一次）
    if ctx == NOT_SUITABLE:
        # 不适合玩梗：无候选概念，n 条正常回复并行出，供用户点选
        cands, related, assigns = None, None, [None] * n
    else:
        used_ctx, pool = pool_for_category(ctx)
        result["context_used_for_pool"] = used_ctx
        result["pool_size"] = len(pool)
        cands = pick_candidates(pool, text)
        result["cand_count"] = len(cands)
        related = related_memes(text)
        result["related_count"] = len(related)
        # 主梗排位（去重）：全库相关在前，池内相关（cands 前段）次之
        merged = list(related)
        seen = {id(m) for m in related}
        for m in cands:
            if id(m) not in seen:
                merged.append(m)
                seen.add(id(m))
        assigns = [merged[i] if i < len(merged) else "fallback" for i in range(n)]
        if prompt_version() == "v2":
            # V2：每条指定一张不同的骨架卡（填充物由生成模型按输入现填）；
            # 卡片不够时用相关梗补位，再不够走兜底向
            picks = list(card_families(merged, text, top=n)) + merged
            assigns = [picks[i] if i < len(picks) else "fallback" for i in range(n)]

    # 第 3 步：并行生成 n 条
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(generate_reply, text, intensity, cands, related, a)
                   for a in assigns]
        replies = [f.result() for f in futures]

    # 结果处理：失败的条丢弃（不占编号），全部失败才算 ERROR
    kept, kept_assign = [], []
    for a, r in zip(assigns, replies):
        if r and r.strip():
            kept.append(r.strip())
            if isinstance(a, dict):
                kept_assign.append((a.get("name") or (a.get("barrage") or ""))[:50])
            else:
                kept_assign.append(a)
    if not kept:
        result["error"] = "生成调用失败（重试3次后仍失败）"
        return result
    result["candidates"] = kept
    result["assigned"] = kept_assign
    return result


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="斗鱼6657串子风格回复生成器")
    ap.add_argument("text", help="输入弹幕文本")
    ap.add_argument("--intensity", type=int, choices=[1, 2, 3], default=2,
                    help="浓度档位：1点到为止 2正常浓度 3火力全开（默认2）")
    args = ap.parse_args()

    res = runmeme(args.text, args.intensity)
    if res.get("error"):
        print(f"ERROR: {res['error']}")
        sys.exit(1)
    print(f"[语境判断] {res['context']}"
          + (f"（检索池：{res['context_used_for_pool']}，{res['pool_size']}条，候选{res['cand_count']}条）"
             if res["context"] != NOT_SUITABLE else "（跳过检索，正常回复）"))
    print(f"[串子回复] {res['reply']}")


if __name__ == "__main__":
    main()
