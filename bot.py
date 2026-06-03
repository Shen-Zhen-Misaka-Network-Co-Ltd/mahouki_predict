import csv
import io
import json
import logging
import os
import statistics
from datetime import datetime, timedelta
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes

CONFIG_PATH = "config.json"
PERIODS_PATH = "periods.json"
BUFFER_PATH = "buffer.json"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("mahouki")

TRIGGER_LABELS = {
    "cold_drink": "冷饮/冰食",
    "spicy": "辛辣食物",
    "stress": "压力/熬夜",
    "fatigue": "疲劳",
    "other": "其他",
    "none": "无明显诱因",
}
SEVERITY_LABELS = {1: "很轻", 2: "轻微", 3: "中等", 4: "较重", 5: "剧烈"}


def load_json(path: str):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class PeriodEngine:
    def __init__(self):
        self.config = load_json(CONFIG_PATH)
        self.periods: dict = load_json(PERIODS_PATH)
        self.buffer: dict = load_json(BUFFER_PATH)

        if "users" not in self.periods:
            self.periods["users"] = {}
        if "messages" not in self.buffer:
            self.buffer["messages"] = []
        if "char_count" not in self.buffer:
            self.buffer["char_count"] = 0

        self._save_periods()
        self._save_buffer()

    # ── persistence ────────────────────────────────────────────────

    def _save_periods(self):
        save_json(PERIODS_PATH, self.periods)

    def _save_buffer(self):
        save_json(BUFFER_PATH, self.buffer)

    def _get_user(self, user_id: str) -> dict:
        uid = str(user_id)
        if uid not in self.periods["users"]:
            self.periods["users"][uid] = {"username": "", "periods": []}
        return self.periods["users"][uid]

    # ── API ────────────────────────────────────────────────────────

    def _api_url(self) -> Optional[str]:
        url = self.config.get("api_url") or self.config.get("base_url", "")
        if not url:
            return None
        url = url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if "/chat/completions" not in url:
            return url + "/chat/completions"
        return url

    def _api_call(self, prompt: str) -> Optional[str]:
        url = self._api_url()
        if not url or not self.config.get("api_key"):
            return None
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.config.get("model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.error("API call failed: %s", e)
            return None

    # ── message pipeline ───────────────────────────────────────────

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.get("admin_ids", [])

    def feed_message(self, text: str, user_id: str, username: str, timestamp: str):
        uid = str(user_id)
        user = self._get_user(uid)
        if username:
            user["username"] = username

        self.buffer["messages"].append({
            "text": text, "user_id": uid,
            "username": username, "timestamp": timestamp,
        })
        self.buffer["char_count"] += len(text)
        self._save_buffer()

        if self.buffer["char_count"] >= self.config.get("buffer_size", 10000):
            return self._analyze_buffer()
        return [], []

    def flush(self):
        return self._analyze_buffer()

    def _parse_llm_json(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            log.warning("Failed to parse LLM: %s", raw[:200])
            return {}

    def _analyze_buffer(self):
        msgs = self.buffer["messages"]
        if not msgs:
            return [], []
        if not self.config.get("api_key") or not self._api_url():
            log.warning("No API configured, clearing buffer")
            self._clear_buffer()
            return [], []

        lines = []
        for m in msgs:
            t = m["timestamp"]
            u = m["username"] or m["user_id"]
            lines.append(f"[{t}] {u}: {m['text']}")
        conversation = "\n".join(lines)

        prompt = (
            "你是一个经期记录分析器。下面是一段群聊记录，请分析并严格按 JSON 输出。\n\n"
            "## 任务一：识别经期自述\n"
            "找出所有自述经期（月经/大姨妈/痛经）的消息。\n"
            "只统计用户**说自己的情况**，排除开玩笑或问别人。\n"
            "对于每条匹配，输出 start_date（根据聊天时间推断经期开始日期）、end_date（如果提到持续几天）。\n"
            "同时判断：\n"
            "  - trigger: 诱因 \"cold_drink\"(冷饮冰食) | \"spicy\"(辛辣) | \"stress\"(压力熬夜) | \"fatigue\"(疲劳) | \"other\" | \"none\"(无明显诱因)\n"
            "  - severity: 严重程度 1(很轻) 2(轻微) 3(中等) 4(较重) 5(剧烈)\n"
            "  - 同时识别经前综合征(PMS)症状如 头痛/疲劳/烦躁/浮肿/长痘/嗜甜/乳房胀痛，如有则记录到 pms_symptoms 数组\n\n"
            "## 任务二：识别纠错消息\n"
            "如果有人说\"还没来\"\"推迟了\"\"延迟了\"\"没来\"等否认预期经期的消息，输出到 corrections 数组。\n\n"
            "请严格按以下JSON格式输出（不要其他内容，不要markdown）：\n"
            '{"periods": [{"user_id":"...","username":"...","start_date":"2026-06-03","end_date":"2026-06-05","trigger":"cold_drink","severity":4,"pms_symptoms":["头痛","疲劳"],"evidence":"吃了冰的痛经了"}],\n'
            '"corrections": [{"user_id":"...","username":"...","expected_date":"2026-07-01","evidence":"还没来"}]}\n\n'
            f"群聊记录：\n{conversation}"
        )

        result = self._api_call(prompt)
        if not result:
            return [], []

        data = self._parse_llm_json(result)
        if not data:
            self._clear_buffer()
            return [], []

        new_periods = []
        for entry in data.get("periods", []):
            uid = str(entry["user_id"])
            user = self._get_user(uid)
            if entry.get("username"):
                user["username"] = entry["username"]

            existing = user["periods"]
            date = entry["start_date"]
            if not any(p["start"] == date for p in existing):
                rec = {
                    "start": date,
                    "end": entry.get("end_date", date),
                    "evidence": entry.get("evidence", ""),
                    "confidence": "auto",
                }
                trigger = entry.get("trigger", "none")
                if trigger and trigger != "none":
                    rec["trigger"] = trigger
                if "severity" in entry:
                    rec["severity"] = entry["severity"]
                if "pms_symptoms" in entry and entry["pms_symptoms"]:
                    rec["pms_symptoms"] = entry["pms_symptoms"]
                existing.append(rec)
                existing.sort(key=lambda x: x["start"])
                new_periods.append(rec)

        corrections = data.get("corrections", [])
        self._save_periods()
        self._clear_buffer()
        log.info("Analyzed buffer: %d new periods, %d corrections", len(new_periods), len(corrections))
        return new_periods, corrections

    def _clear_buffer(self):
        self.buffer["messages"] = []
        self.buffer["char_count"] = 0
        self._save_buffer()

    # ── CRUD ───────────────────────────────────────────────────────

    def add_period(self, user_id: str, start: str, end: str = None,
                   username: str = "", evidence: str = "",
                   trigger: str = None, severity: int = None,
                   confidence: str = "manual"):
        uid = str(user_id)
        user = self._get_user(uid)
        if username:
            user["username"] = username
        if not any(p["start"] == start for p in user["periods"]):
            rec = {
                "start": start, "end": end or start,
                "evidence": evidence, "confidence": confidence,
            }
            if trigger:
                rec["trigger"] = trigger
            if severity:
                rec["severity"] = severity
            user["periods"].append(rec)
            user["periods"].sort(key=lambda x: x["start"])
            self._save_periods()
            return True
        return False

    def del_period(self, user_id: str, start: str) -> bool:
        user = self._get_user(str(user_id))
        before = len(user["periods"])
        user["periods"] = [p for p in user["periods"] if p["start"] != start]
        if len(user["periods"]) < before:
            self._save_periods()
            return True
        return False

    def list_periods(self, user_id: str = None) -> list:
        if user_id:
            uid = str(user_id)
            user = self._get_user(uid)
            return [(uid, user["username"], p) for p in user["periods"]]
        results = []
        for uid, user in self.periods.get("users", {}).items():
            for p in user["periods"]:
                results.append((uid, user["username"], p))
        results.sort(key=lambda x: x[2]["start"])
        return results

    # ── prediction ─────────────────────────────────────────────────

    def _confidence_label(self, std: int) -> str:
        if std <= 1:
            return "高"
        if std <= 3:
            return "中"
        return "低"

    def predict(self, user_id: str) -> Optional[dict]:
        uid = str(user_id)
        user = self._get_user(uid)
        periods = user["periods"]
        if len(periods) < 2:
            return None

        intervals = []
        for i in range(1, len(periods)):
            prev = datetime.strptime(periods[i - 1]["start"], "%Y-%m-%d")
            cur = datetime.strptime(periods[i]["start"], "%Y-%m-%d")
            intervals.append((cur - prev).days)

        avg_cycle = round(statistics.mean(intervals))
        cycle_min = min(intervals)
        cycle_max = max(intervals)
        cycle_std = round(statistics.stdev(intervals)) if len(intervals) > 1 else 0

        durations = []
        for p in periods:
            s = datetime.strptime(p["start"], "%Y-%m-%d")
            e = datetime.strptime(p.get("end", p["start"]), "%Y-%m-%d")
            durations.append(max((e - s).days + 1, 1))
        avg_duration = round(statistics.mean(durations))

        last_start = datetime.strptime(periods[-1]["start"], "%Y-%m-%d")
        next_start = last_start + timedelta(days=avg_cycle)
        next_end = next_start + timedelta(days=avg_duration - 1)
        fertile_start = next_start - timedelta(days=16)
        fertile_end = next_start - timedelta(days=12)

        return {
            "username": user["username"],
            "user_id": uid,
            "avg_cycle_days": avg_cycle,
            "cycle_min_days": cycle_min,
            "cycle_max_days": cycle_max,
            "cycle_std_days": cycle_std,
            "confidence_label": self._confidence_label(cycle_std),
            "avg_duration_days": avg_duration,
            "periods_count": len(periods),
            "last_period_start": periods[-1]["start"],
            "last_period_end": periods[-1].get("end", periods[-1]["start"]),
            "next_period_start": next_start.strftime("%Y-%m-%d"),
            "next_period_end": next_end.strftime("%Y-%m-%d"),
            "fertile_window_start": fertile_start.strftime("%Y-%m-%d"),
            "fertile_window_end": fertile_end.strftime("%Y-%m-%d"),
        }

    # ── insights ────────────────────────────────────────────────────

    def insights(self, user_id: str) -> Optional[dict]:
        uid = str(user_id)
        user = self._get_user(uid)
        periods = user["periods"]
        if not periods:
            return None

        pred = self.predict(uid)
        triggers = {}
        severities = {}
        for p in periods:
            t = p.get("trigger", "none")
            triggers[t] = triggers.get(t, 0) + 1
            s = p.get("severity")
            if s:
                label = SEVERITY_LABELS.get(s, str(s))
                severities[label] = severities.get(label, 0) + 1

        intervals = []
        for i in range(1, len(periods)):
            prev = datetime.strptime(periods[i - 1]["start"], "%Y-%m-%d")
            cur = datetime.strptime(periods[i]["start"], "%Y-%m-%d")
            intervals.append((cur - prev).days)

        return {
            "username": user["username"],
            "user_id": uid,
            "total_periods": len(periods),
            "intervals": intervals[-5:] if intervals else [],
            "triggers": triggers,
            "severities": severities,
            "predict": pred,
        }

    # ── reminders ──────────────────────────────────────────────────

    def get_reminders(self, today: str = None) -> list:
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")
        target = datetime.strptime(today, "%Y-%m-%d")
        remind_before = self.config.get("remind_before_days", 1)
        remind_fertile = self.config.get("remind_fertile", False)
        reminders = []

        for uid, user in self.periods.get("users", {}).items():
            pred = self.predict(uid)
            if pred is None:
                continue

            next_start = datetime.strptime(pred["next_period_start"], "%Y-%m-%d")
            delta = (next_start - target).days

            if delta < 0:
                continue

            if delta == 0:
                reminders.append({
                    "type": "period_start",
                    "message": f"🔴 {pred['username']} 今天可能来月经了，做好准备哦",
                    **pred,
                })
            elif 1 <= delta <= remind_before:
                reminders.append({
                    "type": "period_upcoming",
                    "message": f"🟠 {pred['username']} {delta} 天后（{pred['next_period_start']}）可能来月经",
                    **pred,
                })

            if remind_fertile:
                f_start = datetime.strptime(pred["fertile_window_start"], "%Y-%m-%d")
                f_end = datetime.strptime(pred["fertile_window_end"], "%Y-%m-%d")
                if f_start <= target <= f_end:
                    reminders.append({
                        "type": "fertile_window",
                        "message": f"🟡 {pred['username']} 当前在排卵期窗口（{pred['fertile_window_start']}–{pred['fertile_window_end']}）",
                        **pred,
                    })

        return reminders


engine = PeriodEngine()

# ── helpers ─────────────────────────────────────────────────────────

def _pred_text(pred: dict) -> str:
    return (
        f"📊 {pred['username']} 周期预测\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"记录数: {pred['periods_count']} 次\n"
        f"周期: {pred['avg_cycle_days']} 天 (范围 {pred['cycle_min_days']}–{pred['cycle_max_days']}天, 置信度{pred['confidence_label']})\n"
        f"持续: {pred['avg_duration_days']} 天\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"末次: {pred['last_period_start']} → {pred['last_period_end']}\n"
        f"下次: {pred['next_period_start']} → {pred['next_period_end']}\n"
        f"排卵: {pred['fertile_window_start']} → {pred['fertile_window_end']}"
    )


def _insights_text(data: dict) -> str:
    lines = [f"📈 {data['username']} 周期洞察"]
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"总记录数: {data['total_periods']} 次")
    if data["predict"]:
        p = data["predict"]
        lines.append(f"平均周期: {p['avg_cycle_days']} 天 (±{p['cycle_std_days']})")
    if data["intervals"]:
        lines.append(f"近5次间隔: {' → '.join(str(d) for d in data['intervals'])} 天")
    if data["triggers"]:
        lines.append("常见诱因:")
        for t, c in sorted(data["triggers"].items(), key=lambda x: -x[1]):
            label = TRIGGER_LABELS.get(t, t)
            lines.append(f"  • {label}: {c} 次")
    if data["severities"]:
        lines.append("疼痛分布:")
        for s, c in sorted(data["severities"].items(), key=lambda x: -x[1]):
            lines.append(f"  • {s}: {c} 次")
    return "\n".join(lines)


# ── Telegram handlers ──────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    msg = update.message
    ts = msg.date.isoformat()
    uid = str(msg.from_user.id)
    uname = msg.from_user.full_name or msg.from_user.username or str(msg.from_user.id)

    new_periods, corrections = engine.feed_message(msg.text, uid, uname, ts)
    reply_lines = []

    if new_periods:
        lines = ["📝 检测到新的经期记录："]
        for n in new_periods:
            trigger = TRIGGER_LABELS.get(n.get("trigger", ""), "")
            sev = SEVERITY_LABELS.get(n.get("severity"), "")
            detail = f"  • {n['start']}"
            if trigger:
                detail += f" (诱因: {trigger}"
                if sev:
                    detail += f", {sev}"
                detail += ")"
            lines.append(detail)
            if n.get("evidence"):
                lines.append(f"    证据: {n['evidence'][:80]}")
        reply_lines.extend(lines)

    if corrections:
        for c in corrections:
            reply_lines.append(f"📌 收到纠错: {c.get('evidence', '')}")
            uid_c = str(c["user_id"])
            expected = c.get("expected_date", "")
            if expected:
                user_obj = engine._get_user(uid_c)
                existing = user_obj["periods"]
                existing.append({
                    "start": expected,
                    "end": expected,
                    "evidence": c.get("evidence", ""),
                    "confidence": "corrected",
                    "skipped": True,
                })
                existing.sort(key=lambda x: x["start"])
                engine._save_periods()
                reply_lines.append(f"  ↪ 记录了跳过标记，下次预测会调整周期")

    if reply_lines:
        await msg.reply_text("\n".join(reply_lines), disable_notification=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 魔法期预测 Bot\n\n"
        "监听群聊消息，自动识别经期自述并预测周期。\n\n"
        "指令：\n"
        "/predict — 查看我的预测\n"
        "/insights — 周期洞察\n"
        "/stats — 概览\n"
        "/remind — 今天的提醒\n"
        "/flush — 强制分析缓冲区\n"
        "/export — 导出 CSV\n"
        "/period — 管理经期记录 (仅管理员)"
    )


async def cmd_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    pred = engine.predict(uid)
    if pred:
        await update.message.reply_text(_pred_text(pred))
    else:
        await update.message.reply_text("❌ 数据不足，需要至少 2 条经期记录才能预测。")


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = engine.insights(uid)
    if data:
        await update.message.reply_text(_insights_text(data))
    else:
        await update.message.reply_text("❌ 还没有任何经期记录。")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = engine.periods.get("users", {})
    if not users:
        await update.message.reply_text("还没有任何用户数据。")
        return
    lines = ["📊 数据概览"]
    for uid, u in users.items():
        name = u.get("username", uid)
        count = len(u.get("periods", []))
        pred = engine.predict(uid)
        if pred:
            lines.append(f"  • {name}: {count} 次, 周期 ~{pred['avg_cycle_days']}d ({pred['confidence_label']}), 下次 ~{pred['next_period_start']}")
        else:
            lines.append(f"  • {name}: {count} 次 (暂无预测)")
    await update.message.reply_text("\n".join(lines))


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reminders = engine.get_reminders()
    if reminders:
        for r in reminders:
            await update.message.reply_text(r["message"])
    else:
        await update.message.reply_text("✅ 今天没有任何提醒。")


async def cmd_flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    before = engine.buffer["char_count"]
    new_periods, corrections = engine.flush()
    await update.message.reply_text(
        f"📤 已分析缓冲区 ({before} 字符)，"
        f"发现 {len(new_periods)} 条新记录，{len(corrections)} 条纠错。"
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = engine.list_periods()
    if not rows:
        await update.message.reply_text("❌ 暂无数据可导出。")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "start", "end", "trigger", "severity", "confidence", "evidence"])
    for uid, uname, p in rows:
        w.writerow([
            uid, uname, p["start"], p["end"],
            p.get("trigger", ""), p.get("severity", ""),
            p.get("confidence", ""), p.get("evidence", ""),
        ])
    csv_bytes = buf.getvalue().encode("utf-8")
    await update.message.reply_document(
        document=csv_bytes,
        filename=f"periods_{datetime.now().strftime('%Y%m%d')}.csv",
        caption=f"📤 共 {len(rows)} 条记录",
    )


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not engine.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员可用。")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：\n"
            "/period add <user_id> <start> [end] [trigger] [severity]\n"
            "/period del <user_id> <start>"
        )
        return

    sub = args[0]
    if sub == "add" and len(args) >= 3:
        uid = args[1]
        start = args[2]
        end = args[3] if len(args) > 3 else None
        trigger = args[4] if len(args) > 4 else None
        severity = int(args[5]) if len(args) > 5 else None
        ok = engine.add_period(uid, start, end, trigger=trigger, severity=severity)
        await update.message.reply_text(f"✅ 已{'添加' if ok else '存在，跳过'}")

    elif sub == "del" and len(args) >= 3:
        ok = engine.del_period(args[1], args[2])
        await update.message.reply_text(f"✅ 已{'删除' if ok else '未找到'}")

    else:
        await update.message.reply_text("❌ 参数错误。")


# ── main ────────────────────────────────────────────────────────────

def main():
    token = engine.config.get("bot_token")
    if not token:
        log.error("bot_token 未配置")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("flush", cmd_flush))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("period", cmd_period))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Mahouki Predict bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
