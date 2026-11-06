"""后台任务表：每个调模型的地方一行（全系统模块化第三批，她的决定 7）。

11-06 看完盘点：“其他的按建议，三批并行做。”——决定 7：
用付费额度（或订阅额度用完）时，除了聊天、早上排计划、收邮件分类，其余后台全停，额度恢复后自动补。

规矩（照任务系统 ethan/taskrules.py 的做法：一张表 + 唯一入口 + 测试守卫）：
- 调模型的地方都带一个 purpose（Router.complete / llm.structured 的 purpose），这张表 TABLE 每个 purpose 一行：
  中文名、属于哪类（KIND）、记不记 jobs（定时任务名，经 python3 -m ethan.jobs 留痕；None = 不在定时任务里）、
  怎么起进程（SPAWN）、停了以后怎么补（CATCHUP）、为什么这么归类。
- 唯一的额度检查：ethan/core/router.py 每次调模型前 gate(data_dir, purpose)——这一类在停跑期就抛 QuotaBlocked
  （不调模型、不花钱），并记进 quota-skipped.json，额度恢复后 ethan/catchup.py 按 CATCHUP 补。
- 唯一的起进程入口：ethan/bgwork.py 的 kick(root, 名字, 参数)，名字在 KICKS 里登记（日志、锁、停跑期不起）。
- 守卫：tests/test_bgjobs.py——ethan/ 下调模型的 purpose 不在这张表里、或者自己 subprocess.Popen 起后台进程的，测试不过。
"""

import json
import os
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

# 哪一类（她的决定 7）
ESSENTIAL = "essential"    # 照跑：聊天、早上排计划、收邮件分类（她点名留的三样，加上算在这三样里的）
FOREGROUND = "foreground"  # 照跑：她在工作台点了按钮、正等着结果的（不是“后台”）；或 Claude 会话里手动跑的一次性脚本
BACKGROUND = "background"  # 停：停跑期不调模型，额度恢复后按 CATCHUP 补
KINDS = (ESSENTIAL, FOREGROUND, BACKGROUND)

# 停了以后怎么补（只对 BACKGROUND 有意义）
NEXT = "next"        # 定时任务下一轮自己会再跑（准备员每 2 小时、大脑 30 分钟、大批整理 10 分钟……），不用另补
REOPEN = "reopen"    # 下次打开那一页发现旧了会再起
REPLAY = "replay"    # 被拦下的那次起进程（带参数）记进 quota-skipped.json，恢复后照原样再起一次
# 其余值是 catchup.py 里的补法："journal"（重跑每晚整理）、"diary"、"card"，或 "rerun:<定时任务名>"（照定时任务那条命令再跑一次）
RERUN = {  # rerun:<名字> → 和 deploy/systemd/ethan2-<名字>.service 同一条命令（不含 --data）
    "english": ["-m", "ethan.english"],
    "evals": ["-m", "ethan.evals"],
}

Job = namedtuple("Job", "name kind jobs spawn catchup why")

TABLE = {
    # ---------- 照跑：她点名留的三样 ----------
    "chat": Job("聊天", ESSENTIAL, None, "聊天渠道（Telegram / 微信 / 工作台）", None, "点名照跑"),
    "photo-describe": Job("聊天里看一眼自拍照", ESSENTIAL, None, "聊天渠道", None, "聊天回复的一部分"),
    "proactive": Job("小伴 主动开口（早报、提醒、剧本时刻）", ESSENTIAL, "proactive", "定时 proactive：proactive.gate 放行后 say → reach_out；Grok 回报转告 relay_grok；手动总览", None,
                     "小伴 找她说话算聊天；停了早报、提醒都发不出（要她确认，见设计文档）"),
    "week-review": Job("每周复盘开场白", ESSENTIAL, "proactive", "定时 proactive（say 的 text）", None, "是一次聊天的开场，和主动开口一样（要她确认）"),
    "planner": Job("早上排计划", ESSENTIAL, "planner", "定时 planner；工作台“重排”", None, "点名照跑"),
    "mail-classify": Job("收邮件分类", ESSENTIAL, "mail", "定时 mail（imapmail）；kick mail", None, "点名照跑"),
    "visitor-triage": Job("主页留言分类", ESSENTIAL, None, "收件口收到留言时", None,
                          "和收邮件分类同一类（决定 6 把留言和邮件放在同一个收件台），要她确认"),
    # ---------- 照跑：她点了、在等结果 ----------
    "translate": Job("翻译邮件 / 留言", FOREGROUND, None, "工作台“翻译”；收邮件时顺手翻", None, "她点了在等；收邮件那一步算收邮件"),
    "mail-reply": Job("起草回信", FOREGROUND, None, "kick desk-draft（她点“让 小伴 起草”）", None, "她点了在等"),
    "mail-rewrite": Job("按她的意见改写信", FOREGROUND, None, "工作台请求里", None, "她点了在等"),
    "me-ask": Job("“我”页问一问", FOREGROUND, None, "工作台请求里", None, "她点了在等"),
    "me-compare": Job("“我”页两个回答对比", FOREGROUND, None, "工作台请求里", None, "她点了在等"),
    "diary-fix": Job("按批注改日记那句", FOREGROUND, None, "工作台请求里", None, "她写了批注在等"),
    "statement-import": Job("导入账单", FOREGROUND, None, "工作台上传账单", None, "她上传了在等"),
    "ms-review": Job("里程碑复盘草稿", FOREGROUND, None, "工作台勾复盘 / 打开复盘", None, "她在复盘那一步；模型不可用时按数字写一句"),
    "map-art": Job("地图插画（一次性脚本）", FOREGROUND, None, "Claude 会话手动跑", None, "手动跑的一次性脚本，不是后台"),
    "profile-art": Job("头像插画（一次性脚本）", FOREGROUND, None, "Claude 会话手动跑", None, "手动跑的一次性脚本，不是后台"),
    "prop-regroup": Job("说法重新归类（一次性脚本）", FOREGROUND, None, "Claude 会话手动跑", None, "手动跑的一次性脚本，不是后台"),
    # ---------- 停，恢复后补 ----------
    "memory-nightly": Job("每晚记忆整理", BACKGROUND, "memory", "定时 memory（nightly）", "journal", "决定 7"),
    "fact-ops": Job("每晚整理：新旧说法合并", BACKGROUND, "memory", "每晚整理里", "journal", "决定 7"),
    "profile-block": Job("每晚整理：画像段落", BACKGROUND, "memory", "每晚整理里", "journal", "决定 7"),
    "profile-weekly": Job("每周画像回顾", BACKGROUND, "memory", "每晚整理（周日）", "journal", "决定 7"),
    "style-guide": Job("写作规范归纳", BACKGROUND, "memory", "每晚整理（每周）", "journal", "决定 7"),
    "lesson": Job("挑今天的知识点", BACKGROUND, "memory", "每晚整理里", "journal", "决定 7"),
    "diary": Job("小伴 的日记", BACKGROUND, "memory", "每晚整理；kick diary-backfill；catchup", "diary", "决定 7"),
    "card-director": Job("昨日卡导演", BACKGROUND, None, "早上出卡；工作台现画（kick card）", "card", "决定 7；卡等日记和导演补好再出"),
    "card-line": Job("昨日卡那句话", BACKGROUND, None, "早上出卡；kick card-refresh", "card", "决定 7"),
    "daily-card": Job("昨日卡", BACKGROUND, None, "早上出卡", "card", "决定 7"),
    "english-lesson": Job("英语备课", BACKGROUND, "english", "定时 english", "rerun:english", "决定 7"),
    "weekly-report": Job("周报正文", BACKGROUND, None, "现在并进周日复盘，不单独发", NEXT, "决定 7"),
    "prep": Job("准备员", BACKGROUND, "prep", "定时 prep；kick prep（她提了修改意见）", REPLAY, "决定 7；定时那轮下一轮会再跑"),
    "prep-review": Job("准备员回头看", BACKGROUND, "prep", "定时 prep", NEXT, "决定 7"),
    "prep-help": Job("帮办挑事", BACKGROUND, "prep", "定时 prep", NEXT, "决定 7"),
    "brain-card": Job("大脑专题卡", BACKGROUND, "brain", "定时 brain", NEXT, "决定 7"),
    "brain-enrich": Job("大脑整理", BACKGROUND, "brain", "定时 brain", NEXT, "决定 7"),
    "relations-card": Job("人物卡整理", BACKGROUND, "backlog", "定时 backlog；kick relations", NEXT, "决定 7（11-05 起就停）"),
    "transcript-facts": Job("大批整理：对话记录", BACKGROUND, "backlog", "定时 backlog；kick interviews", NEXT, "决定 7（11-05 起就停）"),
    "interview-facts": Job("访谈整理", BACKGROUND, "backlog", "定时 backlog；kick interviews", NEXT, "决定 7（11-05 起就停）"),
    "file-facts": Job("文件里的说法", BACKGROUND, "backlog", "定时 backlog", NEXT, "决定 7"),
    "extract-realtime": Job("聊天里即时抽取", BACKGROUND, None, "kick extract（小伴 回完话）", REPLAY, "决定 7；恢复后照原来那条再抽"),
    "file-transcribe": Job("她发的文件转文字", BACKGROUND, None, "kick files", REPLAY, "决定 7"),
    "file-digest": Job("她发的文件摘要", BACKGROUND, None, "kick files", REPLAY, "决定 7"),
    "next-season": Job("下个赛季草稿", BACKGROUND, None, "kick next-season", REPLAY, "决定 7"),
    "season-note": Job("赛季页那句结论", BACKGROUND, None, "kick season-note（打开赛季页）；定时 weekly", REOPEN, "决定 7"),
    "life": Job("小伴 自己的一天（剧本）", BACKGROUND, "proactive", "定时 proactive 那一轮里", NEXT, "决定 7；10 分钟后那轮再写"),
    "cal-tidy": Job("整理日历导入的事件", BACKGROUND, "proactive", "定时 proactive 那一轮里", NEXT, "决定 7"),
    "eval-style": Job("题集评测：写作风格", BACKGROUND, "evals", "定时 evals", "rerun:evals", "决定 7"),
    "eval-transcript-facts": Job("题集评测：对话抽取", BACKGROUND, "evals", "定时 evals", "rerun:evals", "决定 7"),
    "memeval-answer": Job("记忆检验题：回答", BACKGROUND, "evals", "定时 evals / Claude 会话", "rerun:evals", "决定 7"),
    "memeval-judge": Job("记忆检验题：判分", BACKGROUND, "evals", "定时 evals / Claude 会话", "rerun:evals", "决定 7"),
    "style-eval": Job("写作规范自检", BACKGROUND, "evals", "定时 evals", "rerun:evals", "决定 7"),
}

# 起后台进程的名字（bgwork.kick 唯一入口）：名字 → (模块, 用到哪些 purpose, 锁（同名文件，N 分钟内不重起；None 不锁）, 说明)
Kick = namedtuple("Kick", "module purposes lock_minutes why")
KICKS = {
    "extract": Kick("ethan.extract", ("extract-realtime",), None, "小伴 回完话，后台抽取她这条"),
    "mail": Kick("ethan.mail", ("mail-classify", "translate"), None, "收件口收到邮件，马上分类"),
    "desk-draft": Kick("ethan.desk", ("mail-reply",), None, "她点“让 小伴 起草”"),
    "diary-backfill": Kick("ethan.diary", ("diary",), 15, "打开日记本发现有天没写"),
    "grokexport": Kick("ethan.grokexport", (), None, "Grok 导出下载导入（不调模型）"),
    "next-season": Kick("ethan.nextseason", ("next-season",), None, "起草 / 重写下个赛季"),
    "files": Kick("ethan.files", ("file-transcribe", "file-digest"), None, "她刚发了文件"),
    "voice": Kick("ethan.voice", (), None, "工作台录音转写（本地转写，不调模型）"),
    "catchup": Kick("ethan.catchup", (), 30, "额度恢复后补（ethan/catchup.py）"),
    "season-note": Kick("ethan.season", ("season-note",), 15, "打开赛季页发现结论旧了"),
    "prep": Kick("ethan.prep", ("prep", "prep-review", "prep-help"), None, "她提了修改意见，马上重做那一件"),
    "interviews": Kick("ethan.interviews", ("transcript-facts", "interview-facts", "file-facts"), None, "收件口来了对话 / 访谈"),
    "relations": Kick("ethan.relations", ("relations-card",), None, "人物卡大批整理"),
    "card": Kick("ethan.cards", ("daily-card", "card-director", "card-line"), None, "工作台现画昨日卡"),
    "card-redo": Kick("ethan.cards", ("daily-card", "card-director", "card-line"), None, "工作台“重画”昨日卡"),
    "replan": Kick("ethan.planner", ("planner",), None, "工作台“重排今天的计划”"),
    "card-refresh": Kick("ethan.cards", ("card-line",), None, "昨天的日记改了，只重出卡上那句话"),
}

SKIPPED = "quota-skipped.json"   # 停跑期被拦下的：{"purposes": {purpose: 第一次被拦的时间}, "replay": [[名字, 参数…], …]}


def job(purpose: str) -> Job:
    """查表；没登记的当后台（守卫测试保证 ethan/ 下不会出现没登记的）。"""
    return TABLE.get(purpose or "chat") or Job(purpose, BACKGROUND, None, "?", NEXT, "没登记，按后台算")


def stops_on_paid(purpose: str) -> bool:
    """用付费额度 / 订阅额度用完时这一类停不停。"""
    return job(purpose).kind == BACKGROUND


def _load(root) -> dict:
    try:
        d = json.loads((Path(root) / SKIPPED).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root, d: dict):
    path = Path(root) / SKIPPED
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def note_skipped(root, purpose: str = None, replay: list = None, now: datetime = None):
    """记一笔“停跑期被拦下了”，额度恢复后 catchup 按表补。写不进去不影响（下一轮定时照样会跑）。"""
    if root is None:
        return
    try:
        d = _load(root)
        if purpose:
            d.setdefault("purposes", {}).setdefault(purpose, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
        if replay and replay not in d.setdefault("replay", []):
            d["replay"].append(replay)
        _save(root, d)
    except OSError:
        pass


def skipped(root) -> dict:
    return _load(root)


def clear_skipped(root, purposes=(), replay=()):
    try:
        d = _load(root)
        for p in purposes:
            (d.get("purposes") or {}).pop(p, None)
        d["replay"] = [r for r in d.get("replay") or [] if r not in replay]
        _save(root, d)
    except OSError:
        pass


def blocked(root, purpose: str, now: datetime = None):
    """这一类现在该停吗：停就返回停跑标记 {"until", "reason"}，否则 None（读不了当没有）。"""
    if root is None or not stops_on_paid(purpose):
        return None
    from .core import quota
    try:
        return quota.blocked(root, now)
    except Exception:
        return None


def gate(root, purpose: str, now: datetime = None):
    """唯一的额度检查（Router 每次调模型前）：该停就记下、抛 QuotaBlocked。"""
    b = blocked(root, purpose, now)
    if b:
        from .core.router import QuotaBlocked
        note_skipped(root, purpose, now=now)
        raise QuotaBlocked(f"{job(purpose).name}：{b['reason']}；到 {b['until']}（UTC）前不跑，恢复后补")


def catchups(root) -> list:
    """额度恢复后该补的（catchup.py 用）：被拦下的 purpose 按表的 CATCHUP 去重。"""
    seen = []
    for p in (_load(root).get("purposes") or {}):
        c = job(p).catchup
        if c and c not in (NEXT, REOPEN, REPLAY) and c not in seen:
            seen.append(c)
    return seen
