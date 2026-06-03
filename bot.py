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

    def _save_periods(self):
        save_json(PERIODS_PATH, self.periods)

    def _save_buffer(self):
        save_json(BUFFER_PATH, self.buffer)

    def _get_user(self, user_id: str) -> dict:
        uid = str(user_id)
        if uid not in self.periods["users"]:
            self.periods["users"][uid] = {"username": "", "periods": []}
        return self.periods["users"][uid]

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
        return []

    def flush(self):
        return self._analyze_buffer()

    def _analyze_buffer(self):
        msgs = self.buffer["messages"]
        if not msgs:
            return []
        if not self.config.get("api_key") or not self._api_url():
            log.warning("No API configured, clearing buffer")
            self._clear_buffer()
            return []

        lines = []
        for m in msgs:
            t = m["timestamp"]
            u = m["username"] or m["user_id"]
            lines.append(f"[{t}] {u}: {m['text']}")
        conversation = "\n".join(lines)

        prompt = (
            "你是一个经期记录分析器。下面是一段群聊记录，请找出所有自述经期（月经/大姨妈/痛经/肚子疼暗示经期）的消息。\n"
            "只统计用户说自己的情况，不包括开玩笑或问别人的。\n"
            "对于每条匹配的消息，判断是否为自述经期：如果是，输出该用户的经期开始日期（根据聊天时间推断）。\n\n"
            "请严格按以下JSON格式输出（不要其他内容）：\n"
            '{"periods": [{"user_id": "...", "username": "...", "date": "2026-06-03", "evidence": "消息原文"}]}\n\n'
            f"群聊记录：\n{conversation}"
        )

        result = self._api_call(prompt)
        if not result:
            return []

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            try:
                start = result.index("{")
                end = result.rindex("}") + 1
                data = json.loads(result[start:end])
            except (ValueError, json.JSONDecodeError):
                log.warning("Failed to parse LLM: %s", result[:200])
                self._clear_buffer()
                return []

        new_periods = []
        for entry in data.get("periods", []):
            uid = str(entry["user_id"])
            user = self._get_user(uid)
            if entry.get("username"):
                user["username"] = entry["username"]

            existing = user["periods"]
            date = entry["date"]
            if not any(p["start"] == date for p in existing):
                existing.append({
                    "start": date,
                    "end": date,
                    "evidence": entry.get("evidence", ""),
                    "confidence": "auto",
                })
                existing.sort(key=lambda x: x["start"])
                new_periods.append(entry)

        self._save_periods()
        self._clear_buffer()
        log.info("Analyzed buffer: %d new periods", len(new_periods))
        return new_periods

    def _clear_buffer(self):
        self.buffer["messages"] = []
        self.buffer["char_count"] = 0
        self._save_buffer()

    # record management

    def add_period(self, user_id: str, start: str, end: str = None,
                   username: str = "", evidence: str = "", confidence: str = "manual"):
        uid = str(user_id)
        user = self._get_user(uid)
        if username:
            user["username"] = username
        if not any(p["start"] == start for p in user["periods"]):
            user["periods"].append({
                "start": start, "end": end or start,
                "evidence": evidence, "confidence": confidence,
            })
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

    # prediction

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
        fertile_start = next_start - timedelta(days=14)
        fertile_end = fertile_start + timedelta(days=5)

        return {
            "username": user["username"],
            "user_id": uid,
            "avg_cycle_days": avg_cycle,
            "cycle_min_days": cycle_min,
            "cycle_max_days": cycle_max,
            "cycle_std_days": cycle_std,
            "avg_duration_days": avg_duration,
            "periods_count": len(periods),
            "last_period_start": periods[-1]["start"],
            "last_period_end": periods[-1].get("end", periods[-1]["start"]),
            "next_period_start": next_start.strftime("%Y-%m-%d"),
            "next_period_end": next_end.strftime("%Y-%m-%d"),
            "fertile_window_start": fertile_start.strftime("%Y-%m-%d"),
            "fertile_window_end": fertile_end.strftime("%Y-%m-%d"),
        }

    def get_reminders(self, today: str = None) -> list:
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")
        target = datetime.strptime(today, "%Y-%m-%d")
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
            elif delta == 1:
                reminders.append({
                    "type": "period_1day_before",
                    "message": f"🟠 {pred['username']} 明天可能来月经，提前准备好卫生用品",
                    **pred,
                })
        return reminders


engine = PeriodEngine()

# ── Telegram handlers ──────────────────────────────────────────────

def _pred_text(pred: dict) -> str:
    return (
        f"📊 {pred['username']} 周期预测\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"记录数: {pred['periods_count']} 次\n"
        f"周期: {pred['avg_cycle_days']} 天 (范围 {pred['cycle_min_days']}–{pred['cycle_max_days']}, σ={pred['cycle_std_days']})\n"
        f"持续: {pred['avg_duration_days']} 天\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"末次: {pred['last_period_start']} → {pred['last_period_end']}\n"
        f"下次: {pred['next_period_start']} → {pred['next_period_end']}\n"
        f"排卵: {pred['fertile_window_start']} → {pred['fertile_window_end']}"
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    msg = update.message
    ts = msg.date.isoformat()
    uid = str(msg.from_user.id)
    uname = msg.from_user.full_name or msg.from_user.username or str(msg.from_user.id)

    new = engine.feed_message(msg.text, uid, uname, ts)
    if new:
        lines = ["📝 检测到新的经期记录："]
        for n in new:
            lines.append(f"  • {n.get('username', uid)} — {n['date']}")
            # 自己说的才通知本人
            if str(n["user_id"]) == uid and n.get("evidence"):
                lines.append(f"    证据: {n['evidence'][:80]}")
        await msg.reply_text("\n".join(lines), disable_notification=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 魔法期预测 Bot\n\n"
        "我在群聊中监听消息，自动识别经期自述并预测周期。\n\n"
        "指令：\n"
        "/predict — 查看我自己的预测\n"
        "/stats — 概览\n"
        "/remind — 今天的提醒\n"
        "/flush — 强制分析缓冲区"
    )


async def cmd_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    pred = engine.predict(uid)
    if pred:
        await update.message.reply_text(_pred_text(pred))
    else:
        await update.message.reply_text("❌ 数据不足，需要至少 2 条经期记录才能预测。")


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
            lines.append(f"  • {name}: {count} 次, 周期 ~{pred['avg_cycle_days']}d, 下次 ~{pred['next_period_start']}")
        else:
            lines.append(f"  • {name}: {count} 次 (暂无预测)")
    await update.message.reply_text("\n".join(lines))


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reminders = engine.get_reminders()
    if reminders:
        for r in reminders:
            await update.message.reply_text(r["message"])
    else:
        await update.message.reply_text("✅ 今天没有任何预测提醒。")


async def cmd_flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    before = engine.buffer["char_count"]
    new_periods = engine.flush()
    await update.message.reply_text(
        f"📤 已分析缓冲区 ({before} 字符)，发现 {len(new_periods)} 条新记录。"
    )


def main():
    token = engine.config.get("bot_token")
    if not token:
        log.error("bot_token 未配置")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("flush", cmd_flush))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Mahouki Predict bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
